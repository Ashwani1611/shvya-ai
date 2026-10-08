# ruff: noqa: F401
"""Focused Operations MCP configuration tools: cadence config."""

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
from apps.integrations.operations.attachment_payloads import (
    AttachmentPayloadError,
    decode_email_attachments,
    redact_attachment_content,
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
    _write_gate,
    _proposal_digest,
    _ensure_approved_proposal_unchanged,
    _reject_secret_like_content,
    _incompatible_existing_attribute_value_count,
    _assert_workflow_safe_attribute_references,
    OperationsToolError,
    OperationsPermissionError,
    OperationsApprovalRequired,
    ToolExecution,
)

def _cadence_schedule(step_data):
    schedule = step_data.get("schedule") or {}
    if not isinstance(schedule, dict):
        raise OperationsToolError("Cadence step schedule must be an object.")
    schedule_type = str(
        schedule.get("type") or FollowupStep.ScheduleType.IMMEDIATE
    ).strip()
    specific_time = schedule.get("time")
    if specific_time:
        try:
            specific_time = dt_time.fromisoformat(str(specific_time))
        except ValueError as exc:
            raise OperationsToolError("Cadence time must use HH:MM or HH:MM:SS.") from exc
    try:
        delay_value = (
            int(schedule["delay_value"])
            if schedule.get("delay_value") is not None
            else None
        )
        recurring_every = (
            int(schedule["recurring_every"])
            if schedule.get("recurring_every") is not None
            else None
        )
        specific_weekday = (
            int(schedule["weekday"])
            if schedule.get("weekday") is not None
            else None
        )
        recurring_weekdays = [
            int(item) for item in (schedule.get("weekdays") or [])
        ]
    except (TypeError, ValueError) as exc:
        raise OperationsToolError("Cadence numeric schedule values are invalid.") from exc
    payload = {
        "schedule_type": schedule_type,
        "delay_value": delay_value,
        "delay_unit": str(schedule.get("delay_unit") or ""),
        "specific_time": specific_time,
        "specific_weekday": specific_weekday,
        "recurring_every": recurring_every,
        "recurring_unit": str(schedule.get("recurring_unit") or ""),
        "recurring_weekdays": recurring_weekdays,
    }
    try:
        _validate_schedule(**payload)
    except FollowupError as exc:
        raise OperationsToolError(str(exc)) from exc
    return payload


def upsert_cadence_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="upsert_cadence_configuration",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Cadence configuration object.")
    _reject_secret_like_content(data, field="cadence")
    sequence_id = (arguments or {}).get("cadence_id")
    sequence = None
    if sequence_id:
        sequence = (
            FollowupSequence.objects.filter(
                pk=_uuid(sequence_id, field="cadence_id"),
                organization=organization,
            )
            .select_related("whatsapp_account")
            .defer("whatsapp_account__access_token")
            .first()
        )
        if sequence is None:
            raise OperationsToolError("Cadence not found in this organization.")

    name = str(data.get("name", sequence.name if sequence else "") or "").strip()
    description = str(
        data.get("description", sequence.description if sequence else "") or ""
    ).strip()
    is_active = data.get(
        "is_active",
        sequence.is_active if sequence is not None else False,
    )
    if not isinstance(is_active, bool):
        raise OperationsToolError("Cadence is_active must be true or false.")
    if not name or len(name) > 255 or len(description) > 300:
        raise OperationsToolError("Cadence name/description is invalid.")
    duplicate = FollowupSequence.objects.filter(
        organization=organization,
        name__iexact=name,
    )
    if sequence is not None:
        duplicate = duplicate.exclude(pk=sequence.pk)
    if duplicate.exists():
        raise OperationsToolError("A Cadence with this name already exists.")

    if sequence is not None and sequence.instagram_account_id:
        raise OperationsToolError("Manage Instagram sequences in the Cadence dashboard.")
    account = sequence.whatsapp_account if sequence else None
    if sequence is not None:
        existing_provider = (
            "api"
            if account is not None and account.connection_type == WhatsAppAccount.ConnectionType.API
            else "hosted"
        )
        requested_provider = data.get("provider")
        if (
            requested_provider is not None
            and str(requested_provider).strip() != existing_provider
        ):
            raise OperationsPermissionError(
                "Cadence provider cannot be changed on an existing Cadence "
                "through the canonical SHVYA service. Create a new Cadence "
                "for a different provider."
            )
        requested_account_id = data.get("whatsapp_account_id")
        if (
            requested_account_id is not None
            and sequence.whatsapp_account_id is not None
            and str(requested_account_id).strip() != str(sequence.whatsapp_account_id)
        ):
            raise OperationsPermissionError(
                "Cadence WhatsApp sender cannot be changed on an existing "
                "Cadence through the canonical SHVYA service. Create a new "
                "Cadence for a different sender."
            )
        provider = existing_provider
    else:
        provider = str(data.get("provider") or ("api" if data.get("whatsapp_account_id") else "hosted")).strip()

    if provider not in {"api", "hosted"}:
        raise OperationsToolError("Cadence provider must be api or hosted.")
    if sequence is None or (sequence.whatsapp_account_id is None and data.get("whatsapp_account_id")):
        account_id = data.get("whatsapp_account_id")
        if account_id:
            account_query = WhatsAppAccount.objects.filter(
                pk=_uuid(account_id, field="whatsapp_account_id"),
                organization=organization,
                is_active=True,
            )
            if provider == "api":
                account_query = account_query.filter(
                    status=WhatsAppAccount.Status.CONNECTED,
                )
            account = account_query.defer("access_token").first()
        if provider == "api" and account is None:
            raise OperationsToolError("A connected WhatsApp API sender is required for API Cadences. For a senderless draft use provider=hosted and is_active=false.")
        if account is not None and provider == "api" and account.connection_type != WhatsAppAccount.ConnectionType.API:
            raise OperationsToolError("The selected account is not a WhatsApp API account.")
        if provider == "hosted":
            if (
                account is not None
                and account.connection_type
                != WhatsAppAccount.ConnectionType.coexisted
            ):
                raise OperationsToolError(
                    "The selected account is not a Hosted/Coexistence WhatsApp account."
                )
            if account is None:
                account = (
                    WhatsAppAccount.objects.filter(
                        organization=organization,
                        connection_type=WhatsAppAccount.ConnectionType.coexisted,
                        is_active=True,
                    )
                    .defer("access_token")
                    .order_by(
                        "business_name",
                        "display_phone_number",
                    )
                    .first()
                )
            if account is None and is_active:
                raise OperationsToolError("An unbound Hosted Cadence must remain inactive until a sender is connected.")

    if account is None and is_active:
        raise OperationsToolError("A Cadence cannot be activated without a bound WhatsApp sender.")

    cadence_before = (
        {
            "id": str(sequence.id),
            "name": sequence.name,
            "description": sequence.description,
            "provider": provider,
            "whatsapp_account_id": str(sequence.whatsapp_account_id) if sequence.whatsapp_account_id else None,
            "is_active": sequence.is_active,
        }
        if sequence is not None
        else None
    )
    cadence_after = {
        "name": name,
        "description": description,
        "provider": provider,
        "whatsapp_account_id": str(account.id) if account else None,
        "is_active": is_active,
    }
    proposal = {
        "organization_id": str(organization.id),
        "cadence_id": str(sequence.id) if sequence else None,
        "before": cadence_before,
        "after": cadence_after,
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
                "operation": "update" if sequence else "create",
                "cadence": {
                    "name": name,
                    "description": description,
                    "provider": provider,
                    "whatsapp_account_id": str(account.id) if account else None,
                },
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": sequence is not None,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence" if sequence else "organization",
            target_id=str(sequence.id) if sequence else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_cadence",
                "mode": "update" if sequence else "create",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            if sequence is not None:
                sequence = (
                    FollowupSequence.objects.select_for_update(of=("self",))
                    .select_related("whatsapp_account")
                    .defer("whatsapp_account__access_token")
                    .filter(
                        pk=sequence.pk,
                        organization=organization,
                    )
                    .first()
                )
                if sequence is None:
                    raise OperationsApprovalRequired(
                        "The Cadence changed or was removed after review. "
                        "Run a fresh dry-run."
                    )
                account = sequence.whatsapp_account
                if account is None and data.get("whatsapp_account_id"):
                    account = WhatsAppAccount.objects.filter(
                        pk=_uuid(data["whatsapp_account_id"], field="whatsapp_account_id"),
                        organization=organization,
                        connection_type=WhatsAppAccount.ConnectionType.coexisted,
                        is_active=True,
                    ).defer("access_token").first()
                    if account is None:
                        raise OperationsApprovalRequired("Requested Hosted sender is unavailable. Run a fresh dry-run.")
                locked_provider = (
                    "api"
                    if account is not None and account.connection_type
                    == WhatsAppAccount.ConnectionType.API
                    else "hosted"
                )
                if locked_provider != provider:
                    raise OperationsApprovalRequired(
                        "The Cadence provider changed after review. "
                        "Run a fresh dry-run."
                    )
            else:
                if provider == "api":
                    account_id = data.get("whatsapp_account_id")
                    account = (
                        WhatsAppAccount.objects.filter(
                            pk=_uuid(
                                account_id,
                                field="whatsapp_account_id",
                            ),
                            organization=organization,
                            is_active=True,
                            status=WhatsAppAccount.Status.CONNECTED,
                            connection_type=WhatsAppAccount.ConnectionType.API,
                        )
                        .defer("access_token")
                        .first()
                        if account_id
                        else None
                    )
                    if account is None:
                        raise OperationsApprovalRequired(
                            "The selected WhatsApp API sender is no longer "
                            "active/connected. Run a fresh dry-run."
                        )
                else:
                    account_id = data.get("whatsapp_account_id")
                    account = (
                        WhatsAppAccount.objects.filter(
                            pk=_uuid(
                                account_id,
                                field="whatsapp_account_id",
                            ),
                            organization=organization,
                            connection_type=WhatsAppAccount.ConnectionType.coexisted,
                            is_active=True,
                        )
                        .defer("access_token")
                        .first()
                        if account_id
                        else None
                    )
                    if account is None:
                        account = (
                            WhatsAppAccount.objects.filter(
                                organization=organization,
                                connection_type=WhatsAppAccount.ConnectionType.coexisted,
                                is_active=True,
                            )
                            .defer("access_token")
                            .order_by(
                                "business_name",
                                "display_phone_number",
                            )
                            .first()
                        )
                    if account is None and is_active:
                        raise OperationsApprovalRequired("A Hosted sender is required before activation. Run a fresh dry-run.")

            if account is None and is_active:
                raise OperationsApprovalRequired("Cannot activate an unbound Cadence.")

            duplicate = FollowupSequence.objects.filter(
                organization=organization,
                name__iexact=name,
            )
            if sequence is not None:
                duplicate = duplicate.exclude(pk=sequence.pk)
            if duplicate.exists():
                raise OperationsToolError(
                    "A Cadence with this name already exists."
                )

            locked_before = (
                {
                    "id": str(sequence.id),
                    "name": sequence.name,
                    "description": sequence.description,
                    "provider": provider,
                    "whatsapp_account_id": str(sequence.whatsapp_account_id) if sequence.whatsapp_account_id else None,
                    "is_active": sequence.is_active,
                }
                if sequence is not None
                else None
            )
            locked_after = {
                "name": name,
                "description": description,
                "provider": provider,
                "whatsapp_account_id": (
                    str(account.id) if account else None
                ),
                "is_active": is_active,
            }
            locked_proposal = {
                "organization_id": str(organization.id),
                "cadence_id": (
                    str(sequence.id)
                    if sequence is not None
                    else None
                ),
                "before": locked_before,
                "after": locked_after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            if sequence is None:
                sequence = create_sequence(
                    organization=organization,
                    created_by=identity.actor,
                    name=name,
                    description=description,
                    whatsapp_account=account,
                    provider=provider,
                    allow_unbound_draft=(provider == "hosted" and account is None),
                )
            else:
                sequence = update_sequence(
                    sequence=sequence,
                    name=name,
                    description=description,
                )

            if sequence.whatsapp_account_id is None and account is not None:
                sequence.whatsapp_account = account
                sequence.save(update_fields=["whatsapp_account", "updated_at"])

            if sequence.is_active != is_active:
                sequence.is_active = is_active
                sequence.save(update_fields=["is_active", "updated_at"])

            sequence.refresh_from_db()
            if (
                sequence.name != name
                or sequence.description != description
                or sequence.whatsapp_account_id != (account.id if account else None)
                or sequence.is_active != is_active
            ):
                raise OperationsToolError(
                    "Cadence configuration verification failed."
                )
            cadence_before = locked_before
            cadence_after = locked_after
    except FollowupError as exc:
        raise OperationsToolError(str(exc)) from exc
    except IntegrityError as exc:
        raise OperationsToolError(
            "Cadence configuration changed concurrently. "
            "Run a fresh dry-run."
        ) from exc
    return ToolExecution(
        data={
            "status": "FIXED",
            "cadence": {
                "id": str(sequence.id),
                "name": sequence.name,
                "step_count": sequence.steps.count(),
                "is_active": sequence.is_active,
                "requires_sender_connection": sequence.whatsapp_account_id is None,
            },
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence",
        target_id=str(sequence.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_cadence",
            "mode": "update" if cadence_before else "create",
            "verification": "passed",
        },
    )


def add_cadence_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="add_cadence_step",
        arguments=arguments,
    )
    sequence = (
        FollowupSequence.objects.filter(
            pk=_uuid((arguments or {}).get("cadence_id"), field="cadence_id"),
            organization=organization,
            is_active=True,
        )
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
        .first()
    )
    if sequence is None:
        raise OperationsToolError("Active Cadence not found in this organization.")
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Cadence step object.")
    _reject_secret_like_content(
        redact_attachment_content(data),
        field="cadence_step",
    )
    step_type = str(data.get("type") or "").strip().lower()
    if step_type not in {"whatsapp", "email", "reminder"}:
        raise OperationsToolError("Cadence step type must be whatsapp, email, or reminder.")
    schedule = _cadence_schedule(data)
    email_attachments = None
    email_attachment_descriptors = []

    template = None
    if step_type == "whatsapp":
        template_id = data.get("template_id")
        template = WhatsAppTemplate.objects.filter(
            pk=_uuid(template_id, field="template_id"),
            organization=organization,
            account=sequence.whatsapp_account,
            status=WhatsAppTemplate.Status.APPROVED,
        ).first()
        if template is None:
            raise OperationsToolError("Approved WhatsApp template not found for this Cadence account.")
    elif step_type == "email":
        if not str(data.get("subject") or "").strip() or not str(data.get("body") or "").strip():
            raise OperationsToolError("Email Cadence steps require subject and body.")
        try:
            email_attachments, email_attachment_descriptors = decode_email_attachments(data)
        except AttachmentPayloadError as exc:
            raise OperationsToolError(str(exc)) from exc
    else:
        if not str(data.get("text") or "").strip():
            raise OperationsToolError("Reminder Cadence steps require reminder text.")

    step_proposal = {
        "cadence_id": str(sequence.id),
        "existing_step_count": sequence.steps.count(),
        "next_position": sequence.steps.count() + 1,
        "step_type": step_type,
        "schedule": schedule,
        "template_id": str(template.id) if template else None,
        "title": str(data.get("title") or ""),
        "subject": str(data.get("subject") or ""),
        "body": str(data.get("body") or ""),
        "text": str(data.get("text") or ""),
        "retry_count": data.get("retry_count", 0),
        "attachments": (
            email_attachment_descriptors
            if step_type == "email"
            else []
        ),
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(
            arguments=arguments,
            proposal=step_proposal,
        )

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "cadence_id": str(sequence.id),
                "step_type": step_type,
                "next_position": sequence.steps.count() + 1,
                "schedule_type": schedule["schedule_type"],
                "attachment_count": len(email_attachment_descriptors),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence",
            target_id=str(sequence.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "add_cadence_step",
                "step_type": step_type,
                "schedule_type": schedule["schedule_type"],
                "proposal_digest": _proposal_digest(step_proposal),
            },
        )

    try:
        with transaction.atomic():
            sequence = (
                FollowupSequence.objects.select_for_update(of=("self",))
                .select_related("whatsapp_account")
                .defer("whatsapp_account__access_token")
                .filter(
                    pk=sequence.pk,
                    organization=organization,
                    is_active=True,
                )
                .first()
            )
            if sequence is None:
                raise OperationsApprovalRequired(
                    "The Cadence changed, was removed, or became inactive. "
                    "Run a fresh dry-run."
                )

            schedule = _cadence_schedule(data)
            template = None
            email_attachments = None
            email_attachment_descriptors = []
            if step_type == "whatsapp":
                template = WhatsAppTemplate.objects.filter(
                    pk=_uuid(
                        data.get("template_id"),
                        field="template_id",
                    ),
                    organization=organization,
                    account=sequence.whatsapp_account,
                    status=WhatsAppTemplate.Status.APPROVED,
                ).first()
                if template is None:
                    raise OperationsApprovalRequired(
                        "The approved WhatsApp template or Cadence sender "
                        "changed after review. Run a fresh dry-run."
                    )
            elif step_type == "email":
                if (
                    not str(data.get("subject") or "").strip()
                    or not str(data.get("body") or "").strip()
                ):
                    raise OperationsToolError(
                        "Email Cadence steps require subject and body."
                    )
                try:
                    email_attachments, email_attachment_descriptors = decode_email_attachments(data)
                except AttachmentPayloadError as exc:
                    raise OperationsToolError(str(exc)) from exc
            else:
                if not str(data.get("text") or "").strip():
                    raise OperationsToolError(
                        "Reminder Cadence steps require reminder text."
                    )

            current_step_count = sequence.steps.count()
            locked_step_proposal = {
                "cadence_id": str(sequence.id),
                "existing_step_count": current_step_count,
                "next_position": current_step_count + 1,
                "step_type": step_type,
                "schedule": schedule,
                "template_id": (
                    str(template.id) if template else None
                ),
                "title": str(data.get("title") or ""),
                "subject": str(data.get("subject") or ""),
                "body": str(data.get("body") or ""),
                "text": str(data.get("text") or ""),
                "retry_count": data.get("retry_count", 0),
                "attachments": (
                    email_attachment_descriptors
                    if step_type == "email"
                    else []
                ),
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_step_proposal,
            )

            if step_type == "whatsapp":
                step = add_whatsapp_step(
                    sequence=sequence,
                    template=template,
                    retry_count=data.get("retry_count", 0),
                    **schedule,
                )
            elif step_type == "email":
                step = add_email_step(
                    sequence=sequence,
                    title=str(data.get("title") or ""),
                    subject=str(data.get("subject") or ""),
                    body=str(data.get("body") or ""),
                    attachments=email_attachments,
                    **schedule,
                )
            else:
                step = add_reminder_step(
                    sequence=sequence,
                    text=str(data.get("text") or ""),
                    **schedule,
                )

            step.refresh_from_db()
            verification_errors = []
            if step.sequence_id != sequence.id:
                verification_errors.append("sequence")
            if step.step_type != step_type:
                verification_errors.append("type")
            if step.position != locked_step_proposal["next_position"]:
                verification_errors.append("position")
            if step.schedule_type != schedule["schedule_type"]:
                verification_errors.append("schedule_type")
            if step.delay_value != schedule["delay_value"]:
                verification_errors.append("delay_value")
            if step.delay_unit != schedule["delay_unit"]:
                verification_errors.append("delay_unit")
            if step.specific_time != schedule["specific_time"]:
                verification_errors.append("specific_time")
            if step.specific_weekday != schedule["specific_weekday"]:
                verification_errors.append("specific_weekday")
            if step.recurring_every != schedule["recurring_every"]:
                verification_errors.append("recurring_every")
            if step.recurring_unit != schedule["recurring_unit"]:
                verification_errors.append("recurring_unit")
            if list(step.recurring_weekdays or []) != list(
                schedule["recurring_weekdays"] or []
            ):
                verification_errors.append("recurring_weekdays")

            if step_type == "whatsapp":
                expected_retry = int(
                    data.get("retry_count", 0) or 0
                )
                if step.whatsapp_template_id != template.id:
                    verification_errors.append("template")
                if step.retry_count != expected_retry:
                    verification_errors.append("retry_count")
            elif step_type == "email":
                expected_title = (
                    str(data.get("title") or "").strip()
                    or f"Email {step.position}"
                )
                if step.title != expected_title:
                    verification_errors.append("title")
                if (
                    step.email_subject
                    != str(data.get("subject") or "").strip()
                ):
                    verification_errors.append("email_subject")
                if (
                    step.email_body
                    != str(data.get("body") or "").strip()
                ):
                    verification_errors.append("email_body")
                stored_attachments = [
                    {
                        "name": item.original_name,
                        "mime_type": item.mime_type,
                        "size": item.size,
                    }
                    for item in step.attachments.order_by("position", "created_at")
                ]
                expected_attachments = [
                    {
                        "name": item["name"],
                        "mime_type": item["mime_type"],
                        "size": item["size"],
                    }
                    for item in email_attachment_descriptors
                ]
                if stored_attachments != expected_attachments:
                    verification_errors.append("email_attachments")
            else:
                if (
                    step.reminder_text
                    != str(data.get("text") or "").strip()
                ):
                    verification_errors.append("reminder_text")

            if verification_errors:
                raise OperationsToolError(
                    "Cadence step verification failed for: "
                    + ", ".join(
                        sorted(set(verification_errors))
                    )
                )
            step_proposal = locked_step_proposal
    except FollowupError as exc:
        raise OperationsToolError(str(exc)) from exc
    except IntegrityError as exc:
        raise OperationsToolError(
            "Cadence steps changed concurrently. Run a fresh dry-run."
        ) from exc

    return ToolExecution(
        data={
            "status": "FIXED",
            "cadence_id": str(sequence.id),
            "step": {
                "id": str(step.id),
                "type": step.step_type,
                "position": step.position,
                "schedule_type": step.schedule_type,
                "attachments": [
                    {
                        "name": item.original_name,
                        "mime_type": item.mime_type,
                        "size": item.size,
                    }
                    for item in step.attachments.order_by("position", "created_at")
                ] if step.step_type == FollowupStep.StepType.EMAIL else [],
            },
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence_step",
        target_id=str(step.id),
        reason=reason,
        audit_summary={
            "operation": "add_cadence_step",
            "cadence_id": str(sequence.id),
            "step_type": step_type,
            "schedule_type": step.schedule_type,
            "verification": "passed",
        },
    )
