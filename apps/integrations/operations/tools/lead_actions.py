# ruff: noqa: F401
"""Focused Operations MCP actions: lead actions."""

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
from apps.crm.services.stage_requirements import missing_attributes
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
    ) = _qualification_contract_snapshot(lead)

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
        ) = _qualification_contract_snapshot(lead)
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
    ) = _qualification_contract_snapshot(lead)
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
