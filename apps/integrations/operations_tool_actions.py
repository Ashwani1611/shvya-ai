# ruff: noqa: F401
"""Lead mutation, qualification repair, AI configuration and analytics tools."""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time as dt_time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import (
    Avg,
    BooleanField,
    Case,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone

from apps.ai_engagement.models import (
    Chunk,
    Document,
    KnowledgeSource,
    OrgInfo,
)
from apps.analytics.models import AnalyticsSettings
from apps.ai_engagement.services.confidentiality import is_sensitive_attribute_definition
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playbook import (
    qualification_questions,
    validate_playbook,
)
from apps.ai_engagement.services.qualification_state import (
    QUALIFIED_STAGE,
    normalize_stage_name,
    requirements_for_lead,
    state_for_lead,
)
from apps.channels.campaign_models import CampaignDelivery
from apps.channels.instagram_models import InstagramMessage
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.crm.models import AttributeDefinition, Lead, LeadActivity, Pipeline, Stage
from apps.followups.models import (
    FollowupExecution,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from apps.hosted_automation.models import HostedAutomationJob
from apps.integrations.diagnostic_auth import sanitize_data, sanitize_text
from apps.integrations.diagnostic_tools import (
    DiagnosticToolError,
    execute_tool as execute_diagnostic_tool,
)
from apps.integrations.operations_approval import approval_fingerprint
from apps.integrations.operations_audit import organization_visible_audit_reason
from apps.integrations.operations_models import (
    OperationsApprovalUse,
    OperationsAuditEvent,
    OperationsSupportSession,
)
from apps.integrations.operations_presence import visible_support_sessions
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_AUDIT_READ,
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_LEAD_ATTRIBUTES_WRITE,
    CAP_LEAD_STAGE_WRITE,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    approval_required,
    effective_capabilities,
    policy_for,
    require_capability,
    OperationsPolicyError,
)
from apps.organizations.access import organization_is_active
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerRun
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    get_pipeline_for_account,
    get_session_settings,
    preview_session_settings_update,
    update_session_settings,
)
from services.crm.attribute_service import (
    MAX_CUSTOM_ATTRIBUTES,
    create_attribute_definition,
    update_attribute_definition,
    update_lead_attribute_values,
)
from services.crm.stage_requirements import missing_attributes
from services.crm.lead_transition import (
    LeadTransitionError,
    move_lead_to_pipeline_stage,
)
from services.followup_service import (
    FollowupError,
    _validate_schedule,
    add_email_step,
    add_reminder_step,
    add_whatsapp_step,
    create_sequence,
    update_sequence,
)
from services.triggers.rules import validate as validate_workflow_rule

from apps.integrations.operations_tools import (
    _uuid,
    _organization_for,
    _reason,
    _require_operations_capability,
    _write_gate,
    _proposal_digest,
    _ensure_approved_proposal_unchanged,
    _tenant_safe_leads,
    _lead,
    _lead_for_write,
    _reject_secret_like_content,
    _normalized_lead_attribute_values,
    _validated_lead_attribute_values,
    _attribute_schema_snapshot,
    _qualification_contract_snapshot,
    _messaging_account,
    _public_messaging_settings,
    OperationsToolError,
    OperationsPermissionError,
    OperationsManualFixRequired,
    OperationsApprovalRequired,
    ToolExecution,
    MESSAGING_AUTOMATION_SETTING_KEYS,
    CONVERSION_BREAKDOWN_LIMIT,
    LOST_REASON_BREAKDOWN_LIMIT,
)

def _operations_facade():
    """Resolve patchable compatibility seams through operations_tools."""
    from apps.integrations import operations_tools

    return operations_tools


def update_messaging_automation_settings(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_MESSAGING_CONFIG_WRITE,
        tool_name="update_messaging_automation_settings",
        arguments=arguments,
    )
    account = _messaging_account(
        organization=organization,
        account_id=(arguments or {}).get(
            "whatsapp_account_id"
        ),
    )
    changes = (arguments or {}).get("changes")
    if not isinstance(changes, dict) or not changes:
        raise OperationsToolError(
            "changes must be a non-empty messaging automation settings object."
        )
    unknown = sorted(
        set(changes)
        - MESSAGING_AUTOMATION_SETTING_KEYS
    )
    if unknown:
        raise OperationsToolError(
            "Unsupported messaging automation settings: "
            + ", ".join(unknown)
        )
    _reject_secret_like_content(
        changes,
        field="messaging_automation",
    )

    try:
        preview = preview_session_settings_update(
            account=account,
            payload=changes,
        )
    except HostedWhatsAppValidationError as exc:
        raise OperationsToolError(str(exc)) from exc

    before = preview["before"]
    after = preview["after"]
    pipeline = preview["pipeline"]
    changed_fields = sorted(
        key
        for key in MESSAGING_AUTOMATION_SETTING_KEYS
        if before.get(key) != after.get(key)
    )
    proposal = {
        "whatsapp_account_id": str(account.id),
        "pipeline_id": str(pipeline.id),
        "before": before,
        "after": after,
    }

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "whatsapp_account_id": str(account.id),
                "pipeline": {
                    "id": str(pipeline.id),
                    "name": pipeline.name,
                },
                "changed_fields": changed_fields,
                "before": _public_messaging_settings(
                    before
                ),
                "after": _public_messaging_settings(
                    after
                ),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_MESSAGING_CONFIG_WRITE,
                ),
                "risk": (
                    "Changes pipeline-linked AI reply, lead creation, bump-up, "
                    "follow-up timing, business-hours or conversation-delay behavior."
                ),
                "reversible": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="whatsapp_account",
            target_id=str(account.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "update_messaging_automation_settings",
                "pipeline_id": str(pipeline.id),
                "changed_fields": changed_fields,
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            organization = Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            account = (
                WhatsAppAccount.objects.select_for_update()
                .select_related("organization")
                .defer("access_token")
                .filter(
                    pk=account.pk,
                    organization=organization,
                    is_active=True,
                )
                .first()
            )
            if account is None:
                raise OperationsApprovalRequired(
                    "The WhatsApp account changed or became inactive. "
                    "Run a fresh dry-run."
                )

            try:
                first_preview = preview_session_settings_update(
                    account=account,
                    payload=changes,
                )
            except HostedWhatsAppValidationError as exc:
                raise OperationsApprovalRequired(
                    "Messaging automation configuration changed after review. "
                    "Run a fresh dry-run."
                ) from exc

            locked_pipeline = (
                Pipeline.objects.select_for_update()
                .filter(
                    pk=first_preview["pipeline"].pk,
                    organization=organization,
                    is_active=True,
                )
                .first()
            )
            if locked_pipeline is None:
                raise OperationsApprovalRequired(
                    "The pipeline linked to this WhatsApp account changed "
                    "after review. Run a fresh dry-run."
                )
            try:
                locked_preview = preview_session_settings_update(
                    account=account,
                    payload=changes,
                )
            except HostedWhatsAppValidationError as exc:
                raise OperationsApprovalRequired(
                    "Messaging automation configuration changed after review. "
                    "Run a fresh dry-run."
                ) from exc
            if locked_preview["pipeline"].id != locked_pipeline.id:
                raise OperationsApprovalRequired(
                    "The pipeline linked to this WhatsApp account changed "
                    "after review. Run a fresh dry-run."
                )

            locked_before = locked_preview["before"]
            locked_after = locked_preview["after"]
            locked_proposal = {
                "whatsapp_account_id": str(account.id),
                "pipeline_id": str(
                    locked_pipeline.id
                ),
                "before": locked_before,
                "after": locked_after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            try:
                updated = update_session_settings(
                    account=account,
                    payload=changes,
                )
            except HostedWhatsAppValidationError as exc:
                raise OperationsToolError(
                    str(exc)
                ) from exc

            verified = get_session_settings(
                account=account
            )
            if verified != updated:
                raise OperationsToolError(
                    "Messaging automation settings verification failed."
                )
            for key in MESSAGING_AUTOMATION_SETTING_KEYS:
                if (
                    key in locked_after
                    and verified.get(key)
                    != locked_after.get(key)
                ):
                    raise OperationsToolError(
                        "Messaging automation settings verification failed "
                        f"for {key}."
                    )
            before = locked_before
            after = locked_after
            pipeline = locked_pipeline
            changed_fields = sorted(
                key
                for key in MESSAGING_AUTOMATION_SETTING_KEYS
                if before.get(key) != after.get(key)
            )
    except IntegrityError as exc:
        raise OperationsToolError(
            "Messaging automation settings changed concurrently. "
            "Run a fresh dry-run."
        ) from exc

    return ToolExecution(
        data={
            "status": "FIXED",
            "whatsapp_account_id": str(account.id),
            "pipeline": {
                "id": str(pipeline.id),
                "name": pipeline.name,
            },
            "changed_fields": changed_fields,
            "settings": _public_messaging_settings(
                after
            ),
            "verification": "passed",
        },
        capability=CAP_MESSAGING_CONFIG_WRITE,
        target_type="whatsapp_account",
        target_id=str(account.id),
        reason=reason,
        audit_summary={
            "operation": "update_messaging_automation_settings",
            "pipeline_id": str(pipeline.id),
            "changed_fields": changed_fields,
            "verification": "passed",
        },
    )


def diagnose_lead_qualification(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )
    lead = _lead(organization, (arguments or {}).get("lead_id"))
    (
        compiled,
        requirements,
        state,
        contract_config,
        criteria,
        completion_target,
    ) = _operations_facade()._qualification_contract_snapshot(lead)

    current_is_qualified = normalize_stage_name(lead.stage.name) == QUALIFIED_STAGE
    qualification_status = str(
        state.get("qualification_status") or ""
    ).casefold()
    criteria_qualified = bool(criteria.get("qualified"))
    target_id = (
        str(completion_target.get("id"))
        if isinstance(completion_target, dict)
        and completion_target.get("id") is not None
        else None
    )

    if current_is_qualified:
        classification = "NO_PROBLEM_FOUND"
        root_cause = "Lead is already in the Qualified stage."
        repair_available = False
    elif qualification_status == "completed" and not criteria_qualified:
        classification = "NO_PROBLEM_FOUND"
        root_cause = (
            "Qualification is completed, but the configured qualification "
            "criteria are not satisfied. A Qualified transition is not expected."
        )
        repair_available = False
    elif (
        qualification_status == "completed"
        and criteria_qualified
        and completion_target is None
    ):
        classification = "ROOT_CAUSE_CONFIRMED"
        root_cause = (
            "Qualification criteria are satisfied, but SHVYA cannot resolve an "
            "authoritative active completion-stage target from the current "
            "Playbook/pipeline configuration."
        )
        repair_available = False
    elif (
        qualification_status == "completed"
        and criteria_qualified
        and completion_target is not None
        and str(lead.stage_id) != target_id
    ):
        classification = "ROOT_CAUSE_CONFIRMED"
        root_cause = (
            "Qualification is completed, criteria are satisfied, and an "
            "authoritative completion target exists, but the lead is not in "
            "that target stage."
        )
        repair_available = True
    elif state.get("all_requirements_answered") and completion_target is not None:
        classification = "LIKELY_CAUSE"
        root_cause = (
            "All configured requirements are answered, but persisted "
            "qualification completion/reconciliation has not completed."
        )
        repair_available = False
    else:
        classification = "INSUFFICIENT_EVIDENCE"
        root_cause = (
            "Qualification is not yet complete, or persisted backend evidence "
            "does not establish a failed qualification transition."
        )
        repair_available = False

    latest_inbound = (
        lead.whatsapp_messages.filter(
            organization=organization,
            account__organization=organization,
            direction="inbound",
        )
        .order_by("-created_at", "-id")
        .first()
    )
    processing = {}
    if latest_inbound is not None and isinstance(latest_inbound.raw_payload, dict):
        raw_processing = latest_inbound.raw_payload.get("shvya_ai_processing")
        if isinstance(raw_processing, dict):
            processing = {
                key: raw_processing.get(key)
                for key in (
                    "processed",
                    "qualification_execution_status",
                    "qualification_reconciliation_status",
                )
                if key in raw_processing
            }

    return ToolExecution(
        data={
            "classification": classification,
            "root_cause": root_cause,
            "repair_available": repair_available,
            "lead": {
                "id": str(lead.id),
                "pipeline_id": str(lead.pipeline_id),
                "pipeline": lead.pipeline.name,
                "stage_id": str(lead.stage_id),
                "stage": lead.stage.name,
            },
            "qualification": {
                "mode": compiled.get("mode"),
                "flow_version": state.get("flow_version") or compiled.get("flow_version"),
                "status": state.get("qualification_status"),
                "result": state.get("qualification_result"),
                "all_requirements_answered": state.get("all_requirements_answered"),
                "answered_requirement_ids": state.get("answered_requirement_ids"),
                "missing_requirement_ids": state.get("missing_requirement_ids"),
                "state_qualified_stage_id": state.get("qualified_stage_id"),
                "state_qualified_stage_name": state.get("qualified_stage_name"),
                "criteria_qualified": criteria_qualified,
                "criteria_reason": criteria.get("reason"),
                "authoritative_target": (
                    {
                        "id": target_id,
                        "name": completion_target.get("name"),
                        "pipeline_id": str(completion_target.get("pipeline_id")),
                        "pipeline": completion_target.get("pipeline__name"),
                    }
                    if isinstance(completion_target, dict)
                    else None
                ),
                "configuration_errors": contract_config.get("errors", []),
                "requirements": [
                    {
                        "id": item.get("id"),
                        "stable_id": item.get("stable_id"),
                        "question": item.get("question"),
                        "required": item.get("required"),
                    }
                    for item in requirements
                ],
            },
            "latest_processing_markers": processing,
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="lead",
        target_id=str(lead.id),
        audit_summary={
            "classification": classification,
            "repair_available": repair_available,
        },
    )


def _validate_operations_stage_move(*, lead, stage):
    if (
        normalize_stage_name(stage.name) == QUALIFIED_STAGE
        and stage.id != lead.stage_id
    ):
        (
            _,
            _,
            qualification_state,
            _,
            qualification_criteria,
            completion_target,
        ) = _operations_facade()._qualification_contract_snapshot(lead)
        if (
            str(
                qualification_state.get(
                    "qualification_status"
                )
                or ""
            ).casefold()
            != "completed"
            or not qualification_criteria.get("qualified")
        ):
            raise OperationsPermissionError(
                "Operations MCP cannot move a lead to Qualified until SHVYA's "
                "backend qualification is completed and its configured criteria "
                "are satisfied."
            )
        if (
            not isinstance(completion_target, dict)
            or str(completion_target.get("id") or "")
            != str(stage.id)
        ):
            raise OperationsPermissionError(
                "The requested Qualified stage is not the authoritative "
                "qualification completion target resolved by SHVYA."
            )

    if stage.id != lead.stage_id:
        missing = list(
            missing_attributes(
                stage,
                (
                    lead.attributes
                    if isinstance(lead.attributes, dict)
                    else {}
                ),
            )
        )
        if missing:
            safe_names = [
                item.name
                for item in missing
                if not is_sensitive_attribute_definition(
                    {
                        "key": item.key,
                        "name": item.name,
                    }
                )
            ]
            if safe_names:
                detail = ", ".join(safe_names[:10])
                raise OperationsPermissionError(
                    "Complete the target stage's required CRM attributes before "
                    f"moving this lead: {detail}."
                )
            raise OperationsManualFixRequired(
                "The target stage has missing sensitive required CRM data. "
                "Operations MCP cannot request or fill credential-like fields; "
                "resolve this manually in SHVYA before moving the lead."
            )


def move_lead_stage(*, identity, arguments, enforce_gate=True):
    organization = _organization_for(identity)
    if enforce_gate:
        dry_run, reason = _write_gate(
            identity=identity,
            organization=organization,
            capability=CAP_LEAD_STAGE_WRITE,
            tool_name="move_lead_stage",
            arguments=arguments,
        )
    else:
        raw_dry_run = (arguments or {}).get("dry_run", True)
        if not isinstance(raw_dry_run, bool):
            raise OperationsToolError("dry_run must be a JSON boolean.")
        dry_run = raw_dry_run
        reason = _reason(arguments, required=True)
    lead = _lead_for_write(
        organization=organization,
        lead_id=(arguments or {}).get("lead_id"),
        dry_run=dry_run,
        arguments=arguments,
    )
    stage = (
        Stage.objects.select_related("pipeline")
        .filter(
            pk=_uuid((arguments or {}).get("target_stage_id"), field="target_stage_id"),
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        )
        .first()
    )
    if stage is None:
        raise OperationsToolError("Active target stage was not found in this organization.")

    _validate_operations_stage_move(
        lead=lead,
        stage=stage,
    )

    before = {
        "pipeline_id": str(lead.pipeline_id),
        "pipeline": lead.pipeline.name,
        "stage_id": str(lead.stage_id),
        "stage": lead.stage.name,
    }
    after = {
        "pipeline_id": str(stage.pipeline_id),
        "pipeline": stage.pipeline.name,
        "stage_id": str(stage.id),
        "stage": stage.name,
    }
    proposal = {"before": before, "after": after}
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "proposed_change": {"before": before, "after": after},
                "risk": "Moves one lead and records canonical CRM pipeline/stage activity.",
                "reversible": True,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_LEAD_STAGE_WRITE,
                ),
            },
            capability=CAP_LEAD_STAGE_WRITE,
            target_type="lead",
            target_id=str(lead.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "move_lead_stage",
                "from_pipeline_id": str(lead.pipeline_id),
                "from_stage_id": str(lead.stage_id),
                "to_pipeline_id": str(stage.pipeline_id),
                "to_stage_id": str(stage.id),
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            lead = (
                _tenant_safe_leads(organization)
                .select_for_update()
                .select_related(
                    "organization",
                    "pipeline",
                    "stage",
                )
                .get(pk=lead.pk)
            )
            stage = (
                Stage.objects.select_related("pipeline")
                .filter(
                    pk=stage.pk,
                    pipeline__organization=organization,
                    pipeline__is_active=True,
                    is_active=True,
                )
                .first()
            )
            if stage is None:
                raise OperationsApprovalRequired(
                    "The approved target stage is no longer active. "
                    "Run a fresh dry-run."
                )

            _validate_operations_stage_move(
                lead=lead,
                stage=stage,
            )
            before = {
                "pipeline_id": str(lead.pipeline_id),
                "pipeline": lead.pipeline.name,
                "stage_id": str(lead.stage_id),
                "stage": lead.stage.name,
            }
            after = {
                "pipeline_id": str(stage.pipeline_id),
                "pipeline": stage.pipeline.name,
                "stage_id": str(stage.id),
                "stage": stage.name,
            }
            proposal = {
                "before": before,
                "after": after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=proposal,
            )

            move_lead_to_pipeline_stage(
                lead=lead,
                pipeline=stage.pipeline,
                stage=stage,
                actor=identity.actor,
            )
            lead.refresh_from_db(
                fields=[
                    "pipeline",
                    "stage",
                    "stage_entered_at",
                    "updated_at",
                ]
            )
            if (
                lead.stage_id != stage.id
                or lead.pipeline_id != stage.pipeline_id
            ):
                raise OperationsToolError(
                    "Stage repair write completed but verification did not "
                    "match the requested state."
                )
    except LeadTransitionError as exc:
        raise OperationsToolError(str(exc)) from exc
    except Lead.DoesNotExist as exc:
        raise OperationsApprovalRequired(
            "The lead changed or was removed after the approved dry-run. "
            "Run a fresh dry-run."
        ) from exc

    return ToolExecution(
        data={
            "status": "FIXED",
            "before": before,
            "after": after,
            "verification": "passed",
        },
        capability=CAP_LEAD_STAGE_WRITE,
        target_type="lead",
        target_id=str(lead.id),
        reason=reason,
        audit_summary={
            "operation": "move_lead_stage",
            "from_pipeline_id": before["pipeline_id"],
            "from_stage_id": before["stage_id"],
            "to_pipeline_id": after["pipeline_id"],
            "to_stage_id": after["stage_id"],
            "verification": "passed",
        },
    )


def repair_qualification_stage(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_LEAD_STAGE_WRITE,
        tool_name="repair_qualification_stage",
        arguments=arguments,
    )
    lead = _lead_for_write(
        organization=organization,
        lead_id=(arguments or {}).get("lead_id"),
        dry_run=dry_run,
        arguments=arguments,
    )
    (
        _,
        _,
        state,
        _,
        criteria,
        completion_target,
    ) = _operations_facade()._qualification_contract_snapshot(lead)
    if str(state.get("qualification_status") or "").casefold() != "completed":
        raise OperationsToolError(
            "Qualification is not completed; SHVYA will not force a "
            "qualification-completion transition."
        )
    if not criteria.get("qualified"):
        raise OperationsPermissionError(
            "Qualification is completed, but configured criteria are not "
            "satisfied. SHVYA will not move this lead to Qualified."
        )
    target_id = (
        completion_target.get("id")
        if isinstance(completion_target, dict)
        else None
    )
    target = (
        Stage.objects.select_related("pipeline")
        .filter(
            pk=target_id,
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        )
        .first()
        if target_id
        else None
    )
    if target is None:
        raise OperationsToolError(
            "No authoritative active qualification completion target can be "
            "resolved from the current Playbook/pipeline configuration."
        )
    nested = dict(arguments or {})
    nested["target_stage_id"] = str(target.id)
    nested["dry_run"] = dry_run
    nested["reason"] = reason
    return move_lead_stage(
        identity=identity,
        arguments=nested,
        enforce_gate=False,
    )


def update_lead_attributes(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_LEAD_ATTRIBUTES_WRITE,
        tool_name="update_lead_attributes",
        arguments=arguments,
    )
    lead = _lead_for_write(
        organization=organization,
        lead_id=(arguments or {}).get("lead_id"),
        dry_run=dry_run,
        arguments=arguments,
    )
    values = (arguments or {}).get("values")
    if not isinstance(values, dict) or not values:
        raise OperationsToolError("values must be a non-empty object keyed by CRM attribute key.")
    _reject_secret_like_content(values, field="lead_attributes")

    definitions = {
        item.key: item
        for item in AttributeDefinition.objects.filter(
            organization=organization,
            is_active=True,
        )
    }
    unknown = sorted(set(values) - set(definitions))
    if unknown:
        raise OperationsToolError(
            "Unknown organization attribute keys: " + ", ".join(unknown[:10])
        )
    sensitive = [
        key
        for key in values
        if is_sensitive_attribute_definition(
            {"key": definitions[key].key, "name": definitions[key].name}
        )
    ]
    if sensitive:
        raise OperationsPermissionError(
            "Sensitive/credential-like attributes cannot be written through Operations MCP."
        )
    normalized_values = _normalized_lead_attribute_values(
        definitions=definitions,
        values=values,
    )

    if dry_run:
        values = _validated_lead_attribute_values(
            definitions=definitions,
            values=normalized_values,
        )
        definition_snapshot = _attribute_schema_snapshot(
            definitions=definitions,
            keys=values,
        )
        before = {
            key: (lead.attributes or {}).get(key)
            for key in values
        }
        proposal = {
            "lead_id": str(lead.id),
            "definitions": definition_snapshot,
            "before": before,
            "after": values,
        }
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "lead_id": str(lead.id),
                "attribute_keys": sorted(values),
                "changed_keys": sorted(
                    key
                    for key, value in values.items()
                    if str(before.get(key) or "") != str(value or "").strip()
                ),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_LEAD_ATTRIBUTES_WRITE,
                ),
                "risk": "Updates only existing non-sensitive organization-defined attributes for this lead.",
                "reversible": True,
            },
            capability=CAP_LEAD_ATTRIBUTES_WRITE,
            target_type="lead",
            target_id=str(lead.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "attribute_keys": sorted(values),
                "operation": "update_lead_attributes",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            lead = (
                _tenant_safe_leads(organization)
                .select_for_update()
                .select_related("organization", "pipeline", "stage")
                .get(pk=lead.pk)
            )
            current_definitions = {
                item.key: item
                for item in AttributeDefinition.objects.filter(
                    organization=organization
                )
            }
            current_unknown = sorted(
                set(values) - set(current_definitions)
            )
            if current_unknown:
                raise OperationsApprovalRequired(
                    "CRM attribute definitions changed after review. "
                    "Run a fresh dry-run before applying this update."
                )
            current_sensitive = [
                key
                for key in values
                if is_sensitive_attribute_definition(
                    {
                        "key": current_definitions[key].key,
                        "name": current_definitions[key].name,
                    }
                )
            ]
            if current_sensitive:
                raise OperationsPermissionError(
                    "A target CRM attribute is now sensitive/credential-like. "
                    "Operations MCP will not write it."
                )

            current_definition_snapshot = _attribute_schema_snapshot(
                definitions=current_definitions,
                keys=normalized_values,
            )
            locked_before = {
                key: (lead.attributes or {}).get(key)
                for key in normalized_values
            }
            locked_proposal = {
                "lead_id": str(lead.id),
                "definitions": current_definition_snapshot,
                "before": locked_before,
                "after": normalized_values,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )
            locked_values = _validated_lead_attribute_values(
                definitions=current_definitions,
                values=normalized_values,
            )

            update_lead_attribute_values(
                organization=organization,
                lead=lead,
                values=locked_values,
            )
            lead.refresh_from_db(
                fields=["attributes", "updated_at"]
            )
            failed = [
                key
                for key, value in locked_values.items()
                if str((lead.attributes or {}).get(key) or "")
                != value
            ]
            if failed:
                raise OperationsToolError(
                    "Attribute write verification failed for: "
                    + ", ".join(failed[:10])
                )
            before = locked_before
    except ValidationError as exc:
        raise OperationsToolError("Lead attribute validation failed.") from exc
    return ToolExecution(
        data={
            "status": "FIXED",
            "lead_id": str(lead.id),
            "updated_keys": sorted(normalized_values),
            "verification": "passed",
        },
        capability=CAP_LEAD_ATTRIBUTES_WRITE,
        target_type="lead",
        target_id=str(lead.id),
        reason=reason,
        audit_summary={
            "attribute_keys": sorted(normalized_values),
            "operation": "update_lead_attributes",
            "verification": "passed",
        },
    )


def update_ai_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="update_ai_configuration",
        arguments=arguments,
    )
    changes = (arguments or {}).get("changes")
    if not isinstance(changes, dict) or not changes:
        raise OperationsToolError("changes must be a non-empty object.")

    allowed = {
        "about",
        "bot_languages",
        "ai_playbook",
        "ai_enabled",
        "bump_up_enabled",
        "bump_up_count",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise OperationsToolError(
            "Unsupported AI configuration fields: " + ", ".join(unknown)
        )

    normalized = {}
    for key, value in changes.items():
        if key in {"about", "bot_languages", "ai_playbook"}:
            text = str(value or "").strip()
            limits = {"about": 12000, "bot_languages": 500, "ai_playbook": 100000}
            if len(text) > limits[key]:
                raise OperationsToolError(f"{key} is too large.")
            redacted = sanitize_text(
                text,
                limit=max(len(text) + 32, 800),
                redact_long=True,
            )
            if redacted != text:
                raise OperationsPermissionError(
                    f"{key} contains credential-like or secret material. "
                    "Do not store secrets in SHVYA AI configuration."
                )
            if key == "ai_playbook":
                try:
                    text = _operations_facade().validate_playbook(text)
                except ValueError as exc:
                    raise OperationsToolError(
                        str(exc)
                    ) from exc
            normalized[key] = text
        elif key in {"ai_enabled", "bump_up_enabled"}:
            if not isinstance(value, bool):
                raise OperationsToolError(f"{key} must be true or false.")
            normalized[key] = value
        elif key == "bump_up_count":
            try:
                count = int(value)
            except (TypeError, ValueError) as exc:
                raise OperationsToolError("bump_up_count must be an integer.") from exc
            if count < 0 or count > 20:
                raise OperationsToolError("bump_up_count must be between 0 and 20.")
            normalized[key] = count

    playbook_validation = None
    if "ai_playbook" in normalized:
        compiled = compile_qualification_requirements(
            qualification_questions(normalized["ai_playbook"])
        )
        playbook_validation = {
            "qualification_mode": compiled.get("mode"),
            "flow_version": compiled.get("flow_version"),
            "requirement_count": len(compiled.get("requirements", [])),
        }

    existing_info = OrgInfo.objects.filter(organization=organization).first()
    info_for_compare = existing_info or OrgInfo(organization=organization)
    changed_fields = [
        key
        for key, value in normalized.items()
        if getattr(info_for_compare, key) != value
    ]
    ai_before = {
        key: getattr(info_for_compare, key)
        for key in normalized
    }
    proposal = {
        "organization_id": str(organization.id),
        "before": ai_before,
        "after": normalized,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(
            arguments=arguments,
            proposal=proposal,
        )

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "changed_fields": changed_fields,
                "playbook_validation": playbook_validation,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "risk": "Changes organization-level customer-facing AI behavior. Existing backend safety, tenant, qualification, and CRM execution rules remain authoritative.",
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="organization",
            target_id=str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "changed_fields": changed_fields,
                "operation": "update_ai_configuration",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            locked_info = (
                OrgInfo.objects.select_for_update()
                .filter(organization=organization)
                .first()
            )
            info_for_locked_compare = (
                locked_info or OrgInfo(organization=organization)
            )
            locked_before = {
                key: getattr(info_for_locked_compare, key)
                for key in normalized
            }
            locked_proposal = {
                "organization_id": str(organization.id),
                "before": locked_before,
                "after": normalized,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            info = locked_info or OrgInfo(
                organization=organization
            )
            for key, value in normalized.items():
                setattr(info, key, value)
            if normalized:
                if locked_info is None:
                    info.save()
                else:
                    info.save(
                        update_fields=[
                            *normalized.keys(),
                            "updated_at",
                        ]
                    )
            info.refresh_from_db()
            verification_failed = [
                key
                for key, value in normalized.items()
                if getattr(info, key) != value
            ]
            if verification_failed:
                raise OperationsToolError(
                    "AI configuration verification failed for: "
                    + ", ".join(verification_failed)
                )
            changed_fields = [
                key
                for key, value in normalized.items()
                if locked_before.get(key) != value
            ]
    except IntegrityError as exc:
        raise OperationsApprovalRequired(
            "Organization AI configuration changed concurrently. "
            "Run a fresh dry-run before applying this update."
        ) from exc
    return ToolExecution(
        data={
            "status": "FIXED",
            "changed_fields": changed_fields,
            "playbook_validation": playbook_validation,
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="organization",
        target_id=str(organization.id),
        reason=reason,
        audit_summary={
            "changed_fields": changed_fields,
            "operation": "update_ai_configuration",
            "verification": "passed",
        },
    )


def get_conversion_analysis(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )

    try:
        days = int((arguments or {}).get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(7, min(days, 90))
    now = timezone.now()
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    tenant_lead_ids = _tenant_safe_leads(
        organization
    ).values("id")

    analytics_settings = (
        AnalyticsSettings.objects.select_related(
            "lead_won_stage__pipeline",
            "lead_lost_stage__pipeline",
        )
        .filter(organization=organization)
        .first()
    )
    configured_won_stage = (
        analytics_settings.lead_won_stage
        if (
            analytics_settings
            and analytics_settings.lead_won_stage_id
            and analytics_settings.lead_won_stage.pipeline.organization_id
            == organization.id
        )
        else None
    )
    configured_lost_stage = (
        analytics_settings.lead_lost_stage
        if (
            analytics_settings
            and analytics_settings.lead_lost_stage_id
            and analytics_settings.lead_lost_stage.pipeline.organization_id
            == organization.id
        )
        else None
    )
    stall_day_threshold = max(
        1,
        min(
            int(
                getattr(
                    analytics_settings,
                    "stall_day_threshold",
                    7,
                )
                or 7
            ),
            90,
        ),
    )

    lost_reason_definition = (
        AttributeDefinition.objects.filter(
            organization=organization,
            is_active=True,
        )
        .filter(
            Q(key__in=["lost_reason", "loss_reason"])
            | Q(name__iexact="Lost Reason")
            | Q(name__iexact="Loss Reason")
        )
        .order_by("display_order", "created_at")
        .first()
    )
    if (
        lost_reason_definition is not None
        and is_sensitive_attribute_definition(
            {
                "key": lost_reason_definition.key,
                "name": lost_reason_definition.name,
            }
        )
    ):
        lost_reason_definition = None

    def first_response_metrics(
        *,
        created,
        channel,
        start_at,
        end_at,
    ):
        if channel == "whatsapp":
            inbound = (
                WhatsAppMessage.objects.filter(
                    organization=organization,
                    account__organization=organization,
                    lead_id=OuterRef("pk"),
                    direction=WhatsAppMessage.Direction.INBOUND,
                    created_at__gte=start_at,
                    created_at__lt=end_at,
                )
                .order_by("created_at", "id")
                .values("created_at")[:1]
            )
            cohort = created.annotate(
                _first_inbound_at=Subquery(inbound)
            )
            outbound = (
                WhatsAppMessage.objects.filter(
                    organization=organization,
                    account__organization=organization,
                    lead_id=OuterRef("pk"),
                    direction=WhatsAppMessage.Direction.OUTBOUND,
                    created_at__gte=OuterRef(
                        "_first_inbound_at"
                    ),
                    created_at__lt=end_at,
                )
                .order_by("created_at", "id")
                .values("created_at")[:1]
            )
        else:
            inbound = (
                InstagramMessage.objects.filter(
                    organization=organization,
                    account__organization=organization,
                    conversation__organization=organization,
                    conversation__account__organization=organization,
                    conversation__lead_id=OuterRef("pk"),
                    direction=InstagramMessage.Direction.INBOUND,
                    created_at__gte=start_at,
                    created_at__lt=end_at,
                )
                .filter(
                    account_id=F(
                        "conversation__account_id"
                    )
                )
                .order_by("created_at", "id")
                .values("created_at")[:1]
            )
            cohort = created.annotate(
                _first_inbound_at=Subquery(inbound)
            )
            outbound = (
                InstagramMessage.objects.filter(
                    organization=organization,
                    account__organization=organization,
                    conversation__organization=organization,
                    conversation__account__organization=organization,
                    conversation__lead_id=OuterRef("pk"),
                    direction=InstagramMessage.Direction.OUTBOUND,
                    created_at__gte=OuterRef(
                        "_first_inbound_at"
                    ),
                    created_at__lt=end_at,
                )
                .filter(
                    account_id=F(
                        "conversation__account_id"
                    )
                )
                .order_by("created_at", "id")
                .values("created_at")[:1]
            )

        cohort = cohort.annotate(
            _first_outbound_at=Subquery(outbound)
        )
        inbound_leads = cohort.filter(
            _first_inbound_at__isnull=False
        ).count()
        responded = (
            cohort.filter(
                _first_inbound_at__isnull=False,
                _first_outbound_at__isnull=False,
            )
            .annotate(
                _first_response_duration=ExpressionWrapper(
                    F("_first_outbound_at")
                    - F("_first_inbound_at"),
                    output_field=DurationField(),
                )
            )
        )
        response_aggregate = responded.aggregate(
            responded_leads=Count("id"),
            average_response=Avg(
                "_first_response_duration"
            ),
        )
        responded_leads = int(
            response_aggregate.get(
                "responded_leads"
            )
            or 0
        )
        average_response = response_aggregate.get(
            "average_response"
        )
        average_seconds = (
            round(
                average_response.total_seconds(),
                2,
            )
            if average_response is not None
            else None
        )
        return {
            "inbound_leads": inbound_leads,
            "responded_leads": responded_leads,
            "response_rate": (
                round(
                    responded_leads / inbound_leads,
                    4,
                )
                if inbound_leads
                else None
            ),
            "average_first_response_seconds": (
                average_seconds
            ),
            "method": (
                "First persisted outbound message after the first persisted "
                "inbound message for the same lead, channel and comparison "
                "window."
            ),
        }

    def window(start_at, end_at):
        created = _tenant_safe_leads(
            organization
        ).filter(
            created_at__gte=start_at,
            created_at__lt=end_at,
        )
        lead_count = created.count()

        source_group_count = (
            created.values("lead_source")
            .distinct()
            .count()
        )
        pipeline_group_count = (
            created.values("pipeline_id")
            .distinct()
            .count()
        )
        stage_group_count = (
            created.values("pipeline_id", "stage_id")
            .distinct()
            .count()
        )
        source_rows = list(
            created.values("lead_source")
            .annotate(count=Count("id"))
            .order_by("-count", "lead_source")[
                :CONVERSION_BREAKDOWN_LIMIT
            ]
        )
        pipeline_rows = list(
            created.values("pipeline_id", "pipeline__name")
            .annotate(count=Count("id"))
            .order_by("-count", "pipeline__name")[
                :CONVERSION_BREAKDOWN_LIMIT
            ]
        )
        stage_rows = list(
            created.values(
                "pipeline__name",
                "stage_id",
                "stage__name",
            )
            .annotate(count=Count("id"))
            .order_by("-count", "pipeline__name", "stage__name")[
                :CONVERSION_BREAKDOWN_LIMIT
            ]
        )

        qualified_activity = (
            LeadActivity.objects.filter(
                organization=organization,
                lead_id__in=created.values("id"),
                topic__in=[
                    LeadActivity.Topic.STAGE_CHANGED,
                    LeadActivity.Topic.PIPELINE_CHANGED,
                ],
                created_at__gte=start_at,
                created_at__lt=end_at,
                new_stage_name__iexact="Qualified",
            )
            .filter(
                Q(new_stage__isnull=True)
                | Q(
                    new_stage__pipeline__organization=organization
                )
            )
            .filter(
                Q(new_pipeline__isnull=True)
                | Q(new_pipeline__organization=organization)
            )
            .filter(
                Q(new_stage__isnull=True)
                | Q(new_pipeline__isnull=True)
                | Q(
                    new_stage__pipeline_id=F(
                        "new_pipeline_id"
                    )
                )
            )
        )
        qualified_ids = qualified_activity.values_list(
            "lead_id",
            flat=True,
        ).distinct()
        qualified_count = qualified_ids.count()
        qualified_by_source = {
            row["lead__lead_source"]: row["count"]
            for row in (
                qualified_activity.values("lead__lead_source")
                .annotate(
                    count=Count(
                        "lead_id",
                        distinct=True,
                    )
                )
            )
        }
        sources = [
            {
                "source": row["lead_source"],
                "lead_count": row["count"],
                "lead_share": (
                    round(row["count"] / lead_count, 4)
                    if lead_count
                    else None
                ),
                "qualified_transitions": qualified_by_source.get(
                    row["lead_source"],
                    0,
                ),
                "qualified_transition_rate": (
                    round(
                        qualified_by_source.get(
                            row["lead_source"],
                            0,
                        )
                        / row["count"],
                        4,
                    )
                    if row["count"]
                    else None
                ),
            }
            for row in source_rows
        ]

        qualification_completed_count = (
            created.filter(
                **{
                    "attributes___shvya_ai_qualification__qualification_status": (
                        "completed"
                    )
                }
            ).count()
        )

        returned_pipeline_ids = [
            row["pipeline_id"]
            for row in pipeline_rows
        ]
        qualified_by_pipeline = {
            row["lead__pipeline_id"]: row["count"]
            for row in (
                qualified_activity.filter(
                    lead__pipeline_id__in=(
                        returned_pipeline_ids
                    )
                )
                .values("lead__pipeline_id")
                .annotate(
                    count=Count(
                        "lead_id",
                        distinct=True,
                    )
                )
            )
        }

        stage_reach_qs = (
            LeadActivity.objects.filter(
                organization=organization,
                lead_id__in=created.values("id"),
                topic__in=[
                    LeadActivity.Topic.STAGE_CHANGED,
                    LeadActivity.Topic.PIPELINE_CHANGED,
                ],
                created_at__gte=start_at,
                created_at__lt=end_at,
                new_pipeline__organization=organization,
                new_stage__pipeline__organization=organization,
                new_stage__pipeline_id=F(
                    "new_pipeline_id"
                ),
            )
            .values(
                "new_pipeline_id",
                "new_pipeline_name",
                "new_stage_id",
                "new_stage_name",
            )
            .annotate(
                lead_count=Count(
                    "lead_id",
                    distinct=True,
                )
            )
            .order_by(
                "-lead_count",
                "new_pipeline_name",
                "new_stage_name",
            )
        )
        stage_reach_group_count = (
            stage_reach_qs.count()
        )
        stage_reach_rows = list(
            stage_reach_qs[
                :CONVERSION_BREAKDOWN_LIMIT
            ]
        )

        messages = (
            WhatsAppMessage.objects.filter(
                organization=organization,
                account__organization=organization,
                created_at__gte=start_at,
                created_at__lt=end_at,
            )
            .filter(
                Q(lead__isnull=True)
                | Q(lead_id__in=tenant_lead_ids)
            )
        )
        outbound = messages.filter(
            direction=WhatsAppMessage.Direction.OUTBOUND,
        )
        outbound_status = {
            row["status"]: row["count"]
            for row in outbound.values("status").annotate(
                count=Count("id")
            )
        }
        successful_outbound = sum(
            outbound_status.get(status, 0)
            for status in (
                WhatsAppMessage.Status.SENT,
                WhatsAppMessage.Status.DELIVERED,
                WhatsAppMessage.Status.READ,
            )
        )
        failed_outbound = outbound_status.get(
            WhatsAppMessage.Status.FAILED,
            0,
        )
        outbound_total = outbound.count()

        first_response = {
            "whatsapp": first_response_metrics(
                created=created,
                channel="whatsapp",
                start_at=start_at,
                end_at=end_at,
            ),
            "instagram": first_response_metrics(
                created=created,
                channel="instagram",
                start_at=start_at,
                end_at=end_at,
            ),
        }

        followups = FollowupExecution.objects.filter(
            organization=organization,
            lead_id__in=tenant_lead_ids,
            state__organization=organization,
            state__lead_id=F("lead_id"),
            state__sequence__organization=organization,
            sequence__organization=organization,
            state__sequence_id=F("sequence_id"),
            sequence__whatsapp_account__organization=organization,
            step__sequence_id=F("sequence_id"),
            scheduled_for__gte=start_at,
            scheduled_for__lt=end_at,
        )
        followup_status = {
            row["status"]: row["count"]
            for row in followups.values("status").annotate(
                count=Count("id")
            )
        }
        followup_total = followups.count()
        cadence_completed = LeadSequenceState.objects.filter(
            organization=organization,
            lead_id__in=tenant_lead_ids,
            sequence__organization=organization,
            sequence__whatsapp_account__organization=organization,
            completed_at__gte=start_at,
            completed_at__lt=end_at,
        ).count()
        cadence_assignments = LeadSequenceState.objects.filter(
            organization=organization,
            lead_id__in=tenant_lead_ids,
            sequence__organization=organization,
            sequence__whatsapp_account__organization=organization,
            assigned_at__gte=start_at,
            assigned_at__lt=end_at,
        )
        cadence_assigned = cadence_assignments.count()
        cadence_completed_from_assignments = (
            cadence_assignments.filter(
                completed_at__isnull=False,
                completed_at__lt=end_at,
            ).count()
        )

        workflow_failures = (
            TriggerRun.objects.filter(
                rule__organization=organization,
                lead_id__in=tenant_lead_ids,
                event__organization=organization,
                event__lead_id=F("lead_id"),
                status__in=["failed", "error"],
                created_at__gte=start_at,
                created_at__lt=end_at,
            )
            .filter(
                Q(message__isnull=True)
                | (
                    Q(
                        message__organization=organization,
                        message__account__organization=organization,
                    )
                    & (
                        Q(message__lead__isnull=True)
                        | Q(message__lead_id=F("lead_id"))
                    )
                )
            )
            .count()
        )
        ai_failures = (
            HostedAutomationJob.objects.filter(
                organization=organization,
                account__organization=organization,
                lead_id__in=tenant_lead_ids,
                source_message__organization=organization,
                source_message__account__organization=organization,
                status=HostedAutomationJob.Status.FAILED,
                created_at__gte=start_at,
                created_at__lt=end_at,
            )
            .filter(
                account_id=F("source_message__account_id"),
            )
            .filter(
                Q(source_message__lead__isnull=True)
                | Q(source_message__lead_id=F("lead_id"))
            )
            .count()
        )

        campaign_deliveries = (
            CampaignDelivery.objects.filter(
                campaign__organization=organization,
                campaign__account__organization=organization,
                campaign__pipeline__organization=organization,
                created_at__gte=start_at,
                created_at__lt=end_at,
            )
            .filter(
                Q(campaign__stage__isnull=True)
                | Q(
                    campaign__stage__pipeline_id=F(
                        "campaign__pipeline_id"
                    )
                )
            )
            .filter(
                Q(lead__isnull=True)
                | Q(lead_id__in=tenant_lead_ids)
            )
        )
        campaign_states = {
            row["state"]: row["count"]
            for row in campaign_deliveries.values(
                "state"
            ).annotate(
                count=Count("id")
            )
        }
        campaign_total = campaign_deliveries.count()
        campaign_delivered = campaign_deliveries.filter(
            delivered_at__isnull=False,
        ).count()
        campaign_replied = campaign_deliveries.filter(
            replied_at__isnull=False,
        ).count()

        lost_transition_count = 0
        lost_reason_rows = []
        lost_reason_total = 0
        if configured_lost_stage is not None:
            lost_activity = LeadActivity.objects.filter(
                organization=organization,
                lead_id__in=created.values("id"),
                topic__in=[
                    LeadActivity.Topic.STAGE_CHANGED,
                    LeadActivity.Topic.PIPELINE_CHANGED,
                ],
                created_at__gte=start_at,
                created_at__lt=end_at,
                new_stage_id=configured_lost_stage.id,
            )
            lost_ids = lost_activity.values(
                "lead_id"
            ).distinct()
            lost_transition_count = lost_ids.count()
            if (
                lost_reason_definition is not None
                and lost_transition_count
            ):
                lost_reason_qs = (
                    _tenant_safe_leads(
                        organization
                    )
                    .filter(id__in=lost_ids)
                    .annotate(
                        _loss_reason=KeyTextTransform(
                            lost_reason_definition.key,
                            "attributes",
                        )
                    )
                    .exclude(
                        _loss_reason__isnull=True
                    )
                    .exclude(_loss_reason="")
                    .values("_loss_reason")
                    .annotate(count=Count("id"))
                    .order_by(
                        "-count",
                        "_loss_reason",
                    )
                )
                lost_reason_total = (
                    lost_reason_qs.count()
                )
                lost_reason_rows = list(
                    lost_reason_qs[
                        :LOST_REASON_BREAKDOWN_LIMIT
                    ]
                )

        return {
            "lead_volume": lead_count,
            "qualified_transitions": qualified_count,
            "qualified_transition_rate": (
                round(qualified_count / lead_count, 4)
                if lead_count
                else None
            ),
            "qualification_completion": {
                "completed_leads": (
                    qualification_completed_count
                ),
                "completion_rate": (
                    round(
                        qualification_completed_count
                        / lead_count,
                        4,
                    )
                    if lead_count
                    else None
                ),
                "method": (
                    "Current persisted SHVYA qualification status for leads "
                    "created in this comparison cohort."
                ),
            },
            "source_mix": sources,
            "pipeline_mix": [
                {
                    "pipeline_id": str(
                        row["pipeline_id"]
                    ),
                    "pipeline": row["pipeline__name"],
                    "lead_count": row["count"],
                    "qualified_transitions": (
                        qualified_by_pipeline.get(
                            row["pipeline_id"],
                            0,
                        )
                    ),
                    "qualified_transition_rate": (
                        round(
                            qualified_by_pipeline.get(
                                row["pipeline_id"],
                                0,
                            )
                            / row["count"],
                            4,
                        )
                        if row["count"]
                        else None
                    ),
                }
                for row in pipeline_rows
            ],
            "stage_mix": [
                {
                    "pipeline": row["pipeline__name"],
                    "stage_id": str(
                        row["stage_id"]
                    ),
                    "stage": row["stage__name"],
                    "lead_count": row["count"],
                }
                for row in stage_rows
            ],
            "stage_conversion_proxy": {
                "method": (
                    "Distinct leads from the created cohort that reached each "
                    "persisted tenant-owned stage through a stage/pipeline "
                    "transition during the comparison window."
                ),
                "stages": [
                    {
                        "pipeline_id": str(
                            row["new_pipeline_id"]
                        ),
                        "pipeline": (
                            row["new_pipeline_name"]
                        ),
                        "stage_id": str(
                            row["new_stage_id"]
                        ),
                        "stage": (
                            row["new_stage_name"]
                        ),
                        "lead_count": row["lead_count"],
                        "reach_rate": (
                            round(
                                row["lead_count"]
                                / lead_count,
                                4,
                            )
                            if lead_count
                            else None
                        ),
                    }
                    for row in stage_reach_rows
                ],
                "stage_group_count": (
                    stage_reach_group_count
                ),
                "stages_returned": len(
                    stage_reach_rows
                ),
                "stages_truncated": (
                    stage_reach_group_count
                    > len(stage_reach_rows)
                ),
            },
            "breakdown_counts": {
                "source_groups": source_group_count,
                "source_groups_returned": len(
                    source_rows
                ),
                "source_groups_truncated": (
                    source_group_count
                    > len(source_rows)
                ),
                "pipeline_groups": pipeline_group_count,
                "pipeline_groups_returned": len(
                    pipeline_rows
                ),
                "pipeline_groups_truncated": (
                    pipeline_group_count
                    > len(pipeline_rows)
                ),
                "stage_groups": stage_group_count,
                "stage_groups_returned": len(
                    stage_rows
                ),
                "stage_groups_truncated": (
                    stage_group_count
                    > len(stage_rows)
                ),
            },
            "messaging": {
                "outbound_total": outbound_total,
                "successful_outbound": successful_outbound,
                "failed_outbound": failed_outbound,
                "success_rate": (
                    round(
                        successful_outbound
                        / outbound_total,
                        4,
                    )
                    if outbound_total
                    else None
                ),
                "status_counts": outbound_status,
                "first_response": first_response,
            },
            "followups": {
                "scheduled_executions": followup_total,
                "status_counts": followup_status,
                "failed": followup_status.get(
                    FollowupExecution.Status.FAILED,
                    0,
                ),
                "blocked": followup_status.get(
                    FollowupExecution.Status.BLOCKED,
                    0,
                ),
                "failure_or_block_rate": (
                    round(
                        (
                            followup_status.get(
                                FollowupExecution.Status.FAILED,
                                0,
                            )
                            + followup_status.get(
                                FollowupExecution.Status.BLOCKED,
                                0,
                            )
                        )
                        / followup_total,
                        4,
                    )
                    if followup_total
                    else None
                ),
                "cadences_completed": cadence_completed,
                "cadences_assigned": cadence_assigned,
                "assigned_cadences_completed_by_period_end": (
                    cadence_completed_from_assignments
                ),
                "assigned_cadence_completion_rate": (
                    round(
                        cadence_completed_from_assignments
                        / cadence_assigned,
                        4,
                    )
                    if cadence_assigned
                    else None
                ),
            },
            "failures": {
                "workflow_failures": workflow_failures,
                "hosted_ai_failures": ai_failures,
            },
            "campaigns": {
                "deliveries": campaign_total,
                "delivered": campaign_delivered,
                "replied": campaign_replied,
                "delivery_rate": (
                    round(
                        campaign_delivered
                        / campaign_total,
                        4,
                    )
                    if campaign_total
                    else None
                ),
                "reply_rate": (
                    round(
                        campaign_replied
                        / campaign_total,
                        4,
                    )
                    if campaign_total
                    else None
                ),
                "state_counts": campaign_states,
            },
            "lost": {
                "lost_stage_configured": (
                    configured_lost_stage
                    is not None
                ),
                "lost_stage_id": (
                    str(configured_lost_stage.id)
                    if configured_lost_stage
                    else None
                ),
                "lost_stage": (
                    configured_lost_stage.name
                    if configured_lost_stage
                    else None
                ),
                "lost_transitions": (
                    lost_transition_count
                ),
                "reason_capture_configured": (
                    lost_reason_definition
                    is not None
                ),
                "reason_attribute_key": (
                    lost_reason_definition.key
                    if lost_reason_definition
                    else None
                ),
                "reason_counts": [
                    {
                        "reason": sanitize_text(
                            row["_loss_reason"],
                            limit=200,
                        ),
                        "count": row["count"],
                    }
                    for row in lost_reason_rows
                ],
                "reason_group_count": (
                    lost_reason_total
                ),
                "reasons_returned": len(
                    lost_reason_rows
                ),
                "reasons_truncated": (
                    lost_reason_total
                    > len(lost_reason_rows)
                ),
            },
        }

    current = window(current_start, now)
    previous = window(
        previous_start,
        current_start,
    )

    observations = []
    comparable_metrics = (
        (
            "lead_volume",
            current["lead_volume"],
            previous["lead_volume"],
        ),
        (
            "qualified_transition_rate",
            current["qualified_transition_rate"],
            previous["qualified_transition_rate"],
        ),
        (
            "qualification_completion_rate",
            current["qualification_completion"][
                "completion_rate"
            ],
            previous["qualification_completion"][
                "completion_rate"
            ],
        ),
        (
            "message_success_rate",
            current["messaging"]["success_rate"],
            previous["messaging"]["success_rate"],
        ),
        (
            "whatsapp_first_response_seconds",
            current["messaging"]["first_response"][
                "whatsapp"
            ]["average_first_response_seconds"],
            previous["messaging"]["first_response"][
                "whatsapp"
            ]["average_first_response_seconds"],
        ),
        (
            "instagram_first_response_seconds",
            current["messaging"]["first_response"][
                "instagram"
            ]["average_first_response_seconds"],
            previous["messaging"]["first_response"][
                "instagram"
            ]["average_first_response_seconds"],
        ),
        (
            "lost_transitions",
            current["lost"]["lost_transitions"],
            previous["lost"]["lost_transitions"],
        ),
        (
            "followup_failure_or_block_rate",
            current["followups"][
                "failure_or_block_rate"
            ],
            previous["followups"][
                "failure_or_block_rate"
            ],
        ),
        (
            "assigned_cadence_completion_rate",
            current["followups"][
                "assigned_cadence_completion_rate"
            ],
            previous["followups"][
                "assigned_cadence_completion_rate"
            ],
        ),
        (
            "workflow_failures",
            current["failures"][
                "workflow_failures"
            ],
            previous["failures"][
                "workflow_failures"
            ],
        ),
        (
            "hosted_ai_failures",
            current["failures"][
                "hosted_ai_failures"
            ],
            previous["failures"][
                "hosted_ai_failures"
            ],
        ),
        (
            "campaign_delivery_rate",
            current["campaigns"]["delivery_rate"],
            previous["campaigns"]["delivery_rate"],
        ),
        (
            "campaign_reply_rate",
            current["campaigns"]["reply_rate"],
            previous["campaigns"]["reply_rate"],
        ),
    )
    for (
        metric,
        current_value,
        previous_value,
    ) in comparable_metrics:
        if current_value != previous_value:
            observations.append(
                {
                    "classification": "Measured",
                    "metric": metric,
                    "current": current_value,
                    "previous": previous_value,
                }
            )

    if (
        current["failures"]["workflow_failures"]
        > previous["failures"]["workflow_failures"]
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": "workflow_failures",
                "current": current["failures"][
                    "workflow_failures"
                ],
                "previous": previous["failures"][
                    "workflow_failures"
                ],
                "note": (
                    "Workflow failures increased during the same comparison "
                    "window. This is correlation, not proof of conversion "
                    "causality."
                ),
            }
        )
    if (
        current["messaging"]["success_rate"]
        is not None
        and previous["messaging"]["success_rate"]
        is not None
        and current["messaging"]["success_rate"]
        < previous["messaging"]["success_rate"]
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": "message_delivery",
                "current": current["messaging"][
                    "success_rate"
                ],
                "previous": previous["messaging"][
                    "success_rate"
                ],
                "note": (
                    "Outbound WhatsApp success rate decreased while the compared "
                    "conversion proxy was measured. Causality requires lead-level "
                    "evidence."
                ),
            }
        )

    current_sources = {
        row["source"]: row
        for row in current["source_mix"]
    }
    previous_sources = {
        row["source"]: row
        for row in previous["source_mix"]
    }
    material_source_shift = False
    for source in sorted(
        set(current_sources)
        | set(previous_sources)
    ):
        current_row = (
            current_sources.get(source) or {}
        )
        previous_row = (
            previous_sources.get(source) or {}
        )
        current_share = (
            current_row.get("lead_share") or 0
        )
        previous_share = (
            previous_row.get("lead_share") or 0
        )
        if (
            abs(
                current_share
                - previous_share
            )
            >= 0.05
        ):
            material_source_shift = True
            observations.append(
                {
                    "classification": "Measured",
                    "metric": "source_mix_share",
                    "source": source,
                    "current": current_share,
                    "previous": previous_share,
                    "note": (
                        "Lead-source share changed by at least "
                        "5 percentage points."
                    ),
                }
            )

        current_rate = current_row.get(
            "qualified_transition_rate"
        )
        previous_rate = previous_row.get(
            "qualified_transition_rate"
        )
        if (
            current_rate is not None
            and previous_rate is not None
            and current_row.get(
                "lead_count",
                0,
            )
            >= 5
            and previous_row.get(
                "lead_count",
                0,
            )
            >= 5
            and current_rate != previous_rate
        ):
            observations.append(
                {
                    "classification": "Measured",
                    "metric": (
                        "source_qualified_transition_rate"
                    ),
                    "source": source,
                    "current": current_rate,
                    "previous": previous_rate,
                }
            )

    if material_source_shift:
        observations.append(
            {
                "classification": "Hypothesis",
                "metric": "source_mix",
                "note": (
                    "Lead-source mix changed materially. Compare source-specific "
                    "qualification rates before attributing any overall conversion "
                    "change to lead quality or marketing source."
                ),
            }
        )

    current_followup_rate = (
        current["followups"][
            "failure_or_block_rate"
        ]
    )
    previous_followup_rate = (
        previous["followups"][
            "failure_or_block_rate"
        ]
    )
    if (
        current_followup_rate is not None
        and previous_followup_rate is not None
        and current_followup_rate
        > previous_followup_rate
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": (
                    "followup_failure_or_block_rate"
                ),
                "current": current_followup_rate,
                "previous": previous_followup_rate,
                "note": (
                    "Failed/blocked follow-up execution increased in the same "
                    "comparison window. Confirm impact on affected leads before "
                    "claiming conversion causality."
                ),
            }
        )

    if (
        current["failures"]["hosted_ai_failures"]
        > previous["failures"]["hosted_ai_failures"]
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": "hosted_ai_failures",
                "current": current["failures"][
                    "hosted_ai_failures"
                ],
                "previous": previous["failures"][
                    "hosted_ai_failures"
                ],
                "note": (
                    "Hosted AI execution failures increased. Inspect affected "
                    "lead/message traces to establish whether they interrupted "
                    "engagement."
                ),
            }
        )

    for metric, current_rate, previous_rate in (
        (
            "campaign_delivery_rate",
            current["campaigns"][
                "delivery_rate"
            ],
            previous["campaigns"][
                "delivery_rate"
            ],
        ),
        (
            "campaign_reply_rate",
            current["campaigns"]["reply_rate"],
            previous["campaigns"]["reply_rate"],
        ),
    ):
        if (
            current_rate is not None
            and previous_rate is not None
            and current_rate < previous_rate
        ):
            observations.append(
                {
                    "classification": "Likely contributor",
                    "metric": metric,
                    "current": current_rate,
                    "previous": previous_rate,
                    "note": (
                        "Campaign performance deteriorated in the same period. "
                        "This is correlation until matched to affected leads and "
                        "downstream stage outcomes."
                    ),
                }
            )

    ageing_leads = _tenant_safe_leads(
        organization
    )
    terminal_stage_ids = [
        stage_id
        for stage_id in (
            (
                configured_won_stage.id
                if configured_won_stage
                else None
            ),
            (
                configured_lost_stage.id
                if configured_lost_stage
                else None
            ),
        )
        if stage_id is not None
    ]
    if terminal_stage_ids:
        ageing_leads = ageing_leads.exclude(
            stage_id__in=terminal_stage_ids
        )

    one_day_ago = now - timedelta(days=1)
    three_days_ago = now - timedelta(days=3)
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)
    thirty_days_ago = now - timedelta(days=30)
    stall_cutoff = now - timedelta(
        days=stall_day_threshold
    )
    ageing = ageing_leads.aggregate(
        active_lead_count=Count("id"),
        stalled_count=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=stall_cutoff
            ),
        ),
        under_1_day=Count(
            "id",
            filter=Q(
                stage_entered_at__gt=one_day_ago
            ),
        ),
        days_1_to_3=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=one_day_ago,
                stage_entered_at__gt=three_days_ago,
            ),
        ),
        days_3_to_7=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=three_days_ago,
                stage_entered_at__gt=seven_days_ago,
            ),
        ),
        days_7_to_14=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=seven_days_ago,
                stage_entered_at__gt=fourteen_days_ago,
            ),
        ),
        days_14_to_30=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=fourteen_days_ago,
                stage_entered_at__gt=thirty_days_ago,
            ),
        ),
        days_30_plus=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=thirty_days_ago
            ),
        ),
        oldest_stage_entered_at=Min(
            "stage_entered_at"
        ),
    )
    active_lead_count = int(
        ageing.get("active_lead_count") or 0
    )
    stalled_count = int(
        ageing.get("stalled_count") or 0
    )
    oldest_stage_entered_at = ageing.get(
        "oldest_stage_entered_at"
    )
    oldest_stage_age_days = (
        round(
            (
                now
                - oldest_stage_entered_at
            ).total_seconds()
            / 86400,
            2,
        )
        if oldest_stage_entered_at
        else None
    )
    fixed_seven_day_stale = (
        _tenant_safe_leads(
            organization
        )
        .filter(
            stage_entered_at__lt=(
                now - timedelta(days=7)
            )
        )
        .count()
    )

    return ToolExecution(
        data={
            "comparison": {
                "days_per_period": days,
                "current_period": {
                    "start": (
                        current_start.isoformat()
                    ),
                    "end": now.isoformat(),
                    **current,
                },
                "previous_period": {
                    "start": (
                        previous_start.isoformat()
                    ),
                    "end": (
                        current_start.isoformat()
                    ),
                    **previous,
                },
            },
            "current_snapshot": {
                "leads_in_current_stage_for_7_plus_days": (
                    fixed_seven_day_stale
                ),
                "lead_ageing": {
                    "scope": "current_non_terminal_stage_snapshot",
                    "stall_day_threshold": (
                        stall_day_threshold
                    ),
                    "threshold_source": (
                        "analytics_settings"
                        if analytics_settings
                        else "default"
                    ),
                    "active_lead_count": (
                        active_lead_count
                    ),
                    "stalled_count": stalled_count,
                    "stalled_rate": (
                        round(
                            stalled_count
                            / active_lead_count,
                            4,
                        )
                        if active_lead_count
                        else None
                    ),
                    "oldest_stage_age_days": (
                        oldest_stage_age_days
                    ),
                    "buckets": {
                        "under_1_day": (
                            ageing["under_1_day"]
                        ),
                        "days_1_to_3": (
                            ageing["days_1_to_3"]
                        ),
                        "days_3_to_7": (
                            ageing["days_3_to_7"]
                        ),
                        "days_7_to_14": (
                            ageing["days_7_to_14"]
                        ),
                        "days_14_to_30": (
                            ageing["days_14_to_30"]
                        ),
                        "days_30_plus": (
                            ageing["days_30_plus"]
                        ),
                    },
                    "excluded_terminal_stage_ids": [
                        str(item)
                        for item in terminal_stage_ids
                    ],
                },
            },
            "observations": observations,
            "limitations": [
                (
                    "Qualified transition rate is a CRM transition proxy: it uses "
                    "persisted activity and lead volume, not a claim that every "
                    "Qualified lead converted to revenue."
                ),
                (
                    "Pipeline/stage mix describes the current CRM location of leads "
                    "created in each comparison period; it is not a historical "
                    "stage snapshot. Pipeline Qualified rates therefore use each "
                    "lead's current pipeline as the cohort grouping."
                ),
                (
                    "Qualification completion uses each cohort lead's current "
                    "persisted qualification status. It does not reconstruct the "
                    "status as it existed at the historical period end."
                ),
                (
                    "Stage conversion is reported as a persisted stage-reach proxy "
                    "from CRM transition activity, not as revenue conversion."
                ),
                (
                    "First-response metrics measure the first persisted outbound "
                    "after the first persisted inbound for the same lead/channel "
                    "inside each period. They do not infer whether the responder "
                    "was AI or a human."
                ),
                (
                    "Lead ageing is a current stage-age snapshot. Historical stage "
                    "age is not reconstructed for the previous comparison period."
                ),
                (
                    "Lost reasons are reported only when Analytics has an "
                    "organization-owned lost stage and CRM has a non-sensitive "
                    "Lost Reason/Loss Reason attribute. Missing reason capture is "
                    "reported as unavailable rather than guessed."
                ),
                (
                    "Likely-contributor labels are correlation, not proven "
                    "causality."
                ),
            ],
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "period_days": days,
            "observation_count": len(
                observations
            ),
            "first_response_metrics": True,
            "lead_ageing_snapshot": True,
            "lost_reason_analysis": True,
        },
    )

def get_operations_audit(*, identity, arguments):
    arguments = arguments or {}
    scope = str(
        arguments.get("scope") or "organization"
    ).strip().casefold()
    if scope not in {"organization", "platform"}:
        raise OperationsToolError(
            "scope must be organization or platform."
        )

    if scope == "platform":
        if identity.role != ROLE_SUPERADMIN:
            raise OperationsPermissionError(
                "Platform Operations audit is available only to SHVYA Superadmin."
            )
        organization = None
        _require_operations_capability(
            identity=identity,
            organization=None,
            capability=CAP_AUDIT_READ,
        )
        rows = OperationsAuditEvent.objects.filter(
            organization__isnull=True
        ).select_related("actor", "support_session")
    else:
        organization = _organization_for(identity)
        _require_operations_capability(
            identity=identity,
            organization=organization,
            capability=CAP_AUDIT_READ,
        )
        rows = OperationsAuditEvent.objects.filter(
            organization=organization
        ).select_related("actor", "support_session")

    try:
        limit = int(arguments.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 100))

    audit_event_id = str(
        arguments.get("audit_event_id") or ""
    ).strip()
    if audit_event_id:
        rows = rows.filter(
            pk=_uuid(
                audit_event_id,
                field="audit_event_id",
            )
        )

    tool_name = str(
        arguments.get("tool_name") or ""
    ).strip()
    if tool_name:
        rows = rows.filter(tool_name=tool_name[:100])

    target_type = str(
        arguments.get("target_type") or ""
    ).strip()
    if target_type:
        rows = rows.filter(
            target_type=target_type[:80]
        )

    target_id = str(
        arguments.get("target_id") or ""
    ).strip()
    if target_id:
        rows = rows.filter(target_id=target_id[:100])

    support_context_id = str(
        arguments.get("support_context_id") or ""
    ).strip()
    if support_context_id:
        rows = rows.filter(
            support_session_id=_uuid(
                support_context_id,
                field="support_context_id",
            )
        )

    outcome = str(
        arguments.get("outcome") or ""
    ).strip()
    valid_outcomes = {
        choice
        for choice, _label
        in OperationsAuditEvent.Outcome.choices
    }
    if outcome:
        if outcome not in valid_outcomes:
            raise OperationsToolError(
                "outcome must be one of: "
                + ", ".join(sorted(valid_outcomes))
            )
        rows = rows.filter(outcome=outcome)

    event_count = rows.count()
    rows = list(
        rows.order_by("-created_at")[:limit]
    )
    return ToolExecution(
        data={
            "scope": scope,
            "organization": (
                {
                    "id": str(organization.id),
                    "name": organization.name,
                }
                if organization is not None
                else None
            ),
            "events": [
                {
                    "id": str(item.id),
                    "actor": (
                        item.actor.name
                        if item.actor
                        else "Former user"
                    ),
                    "role": item.role,
                    "tool": item.tool_name,
                    "capability": item.capability,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "support_context_id": (
                        str(item.support_session_id)
                        if item.support_session_id
                        else None
                    ),
                    "reason": (
                        organization_visible_audit_reason(item)
                        if identity.role == ROLE_ORGANIZATION_ADMIN
                        else item.reason
                    ),
                    "outcome": item.outcome,
                    "change_summary": item.change_summary,
                    "error_code": item.error_code,
                    "created_at": item.created_at.isoformat(),
                }
                for item in rows
            ],
            "count": len(rows),
            "event_count": event_count,
            "events_returned": len(rows),
            "events_truncated": event_count > len(rows),
            "filters": {
                "audit_event_id": audit_event_id or None,
                "tool_name": tool_name or None,
                "target_type": target_type or None,
                "target_id": target_id or None,
                "support_context_id": (
                    support_context_id or None
                ),
                "outcome": outcome or None,
            },
        },
        capability=CAP_AUDIT_READ,
        target_type=(
            "platform"
            if scope == "platform"
            else "organization"
        ),
        target_id=(
            ""
            if scope == "platform"
            else str(organization.id)
        ),
        audit_summary={
            "scope": scope,
            "result_count": len(rows),
            "event_count": event_count,
            "truncated": event_count > len(rows),
            "filtered": any(
                (
                    audit_event_id,
                    tool_name,
                    target_type,
                    target_id,
                    support_context_id,
                    outcome,
                )
            ),
        },
    )

