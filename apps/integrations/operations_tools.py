from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time as dt_time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.confidentiality import is_sensitive_attribute_definition
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playbook import qualification_questions
from apps.ai_engagement.services.qualification_state import (
    QUALIFIED_STAGE,
    normalize_stage_name,
    requirements_for_lead,
    state_for_lead,
)
from apps.channels.campaign_models import CampaignDelivery
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.crm.models import AttributeDefinition, Lead, LeadActivity, Pipeline, Stage
from apps.followups.models import (
    FollowupExecution,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from apps.hosted_automation.models import HostedAutomationJob
from apps.integrations.diagnostic_auth import sanitize_text
from apps.integrations.diagnostic_tools import (
    DiagnosticToolError,
    execute_tool as execute_diagnostic_tool,
)
from apps.integrations.operations_approval import approval_fingerprint
from apps.integrations.operations_models import (
    OperationsApprovalUse,
    OperationsAuditEvent,
    OperationsSupportSession,
)
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_AUDIT_READ,
    CAP_AUTOMATION_CONFIG_WRITE,
    CAP_CRM_CONFIG_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_LEAD_ATTRIBUTES_WRITE,
    CAP_LEAD_STAGE_WRITE,
    CAP_ORGANIZATION_READ,
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
from services.crm.attribute_service import (
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


DIAGNOSTIC_TOOL_NAMES = {
    "find_leads",
    "get_lead_snapshot",
    "get_conversation",
    "trace_message",
    "get_ai_diagnostics",
    "get_integration_health",
    "get_workflow_trace",
    "get_recent_errors",
    "get_runtime_health",
}


class OperationsToolError(ValueError):
    code = "operations_input_error"
    outcome = OperationsAuditEvent.Outcome.ERROR


class OperationsPermissionError(OperationsToolError):
    code = "operations_permission_denied"
    outcome = OperationsAuditEvent.Outcome.DENIED


class OperationsSuperadminRequired(OperationsPermissionError):
    code = "operations_superadmin_required"


class OperationsManualFixRequired(OperationsToolError):
    code = "operations_manual_fix_required"


class OperationsApprovalRequired(OperationsToolError):
    code = "operations_approval_required"
    outcome = OperationsAuditEvent.Outcome.APPROVAL_REQUIRED


@dataclass
class ToolExecution:
    data: dict
    capability: str = ""
    target_type: str = ""
    target_id: str = ""
    reason: str = ""
    outcome: str = OperationsAuditEvent.Outcome.SUCCESS
    audit_summary: dict | None = None


def _uuid(value, *, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise OperationsToolError(f"{field} must be a valid UUID.") from exc


def _organization_for(identity, *, required=True):
    organization = (
        identity.active_organization
        if identity.role == ROLE_SUPERADMIN
        else identity.organization
    )
    if required and organization is None:
        raise OperationsToolError(
            "Select an organization support context before using this tool."
        )
    if required and not organization_is_active(organization):
        raise OperationsPermissionError(
            "The active organization is disabled. Customer-specific Operations "
            "are blocked until the organization is re-enabled or Superadmin "
            "clears/selects another support context."
        )
    return organization


def _reason(arguments, *, required=False):
    value = str((arguments or {}).get("reason") or "").strip()
    if required and len(value) < 8:
        raise OperationsToolError(
            "A specific reason of at least 8 characters is required for this mutation."
        )
    safe = sanitize_text(
        value,
        limit=max(len(value) + 32, 800),
        redact_long=False,
    )
    if safe != value:
        raise OperationsPermissionError(
            "Action reason contains credential-like or secret material. "
            "Use a non-secret operational reason."
        )
    return value[:500]


APPROVAL_RECEIPT_TTL = timedelta(minutes=30)


def _validated_approval_event(
    *,
    identity,
    organization,
    capability,
    tool_name,
    arguments,
):
    raw_event_id = str(
        (arguments or {}).get("approval_event_id") or ""
    ).strip()
    if not raw_event_id:
        raise OperationsApprovalRequired(
            "Approval is required. Run the exact dry-run first, obtain its "
            "approval_event_id, then re-run with approved=true and that event ID."
        )
    try:
        event_id = uuid.UUID(raw_event_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise OperationsApprovalRequired(
            "approval_event_id must be the UUID returned by the matching SHVYA dry-run."
        ) from exc

    event = OperationsAuditEvent.objects.filter(
        pk=event_id,
        actor=identity.actor,
        role=identity.role,
        organization=organization,
        tool_name=tool_name,
        capability=capability,
        outcome=OperationsAuditEvent.Outcome.DRY_RUN,
        request_fingerprint=approval_fingerprint(arguments),
        created_at__gte=timezone.now() - APPROVAL_RECEIPT_TTL,
    ).first()
    if event is None:
        raise OperationsApprovalRequired(
            "The approval receipt is missing, expired, belongs to another actor/"
            "organization/tool, or does not match this exact proposed change. "
            "Run a new dry-run and approve that result."
        )

    try:
        OperationsApprovalUse.objects.create(
            approval_event=event,
        )
    except IntegrityError as exc:
        raise OperationsApprovalRequired(
            "This approval receipt has already been claimed by an execution "
            "attempt. Run a new dry-run before another mutation."
        ) from exc
    return event


def _require_operations_capability(
    *,
    identity,
    organization,
    capability,
):
    try:
        require_capability(
            role=identity.role,
            organization=organization,
            capability=capability,
        )
    except OperationsPolicyError as exc:
        if identity.role != ROLE_SUPERADMIN:
            raise OperationsSuperadminRequired(
                f"Capability '{capability}' is not enabled for this organization. "
                "SHVYA Superadmin must enable it in the External AI Operations policy."
            ) from exc
        raise OperationsPermissionError(str(exc)) from exc



def _write_gate(
    *,
    identity,
    organization,
    capability,
    tool_name,
    arguments,
):
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=capability,
    )

    if "operations.write" not in identity.scopes:
        raise OperationsPermissionError(
            "This OAuth token does not include operations.write."
        )

    raw_dry_run = (arguments or {}).get("dry_run", True)
    raw_approved = (arguments or {}).get("approved", False)
    if not isinstance(raw_dry_run, bool):
        raise OperationsToolError("dry_run must be a JSON boolean.")
    if not isinstance(raw_approved, bool):
        raise OperationsToolError("approved must be a JSON boolean.")
    dry_run = raw_dry_run
    approved = raw_approved
    reason = _reason(arguments, required=True)
    needs_approval = approval_required(
        role=identity.role,
        organization=organization,
        capability=capability,
    )
    if not dry_run and needs_approval:
        if not approved:
            raise OperationsApprovalRequired(
                "Approval is required. Run the exact dry-run first and obtain "
                "human approval before execution."
            )
        _validated_approval_event(
            identity=identity,
            organization=organization,
            capability=capability,
            tool_name=tool_name,
            arguments=arguments,
        )
    return dry_run, reason


def _proposal_digest(payload) -> str:
    """Short safe digest of backend-resolved mutation state."""

    return approval_fingerprint(
        payload if isinstance(payload, dict) else {"value": payload}
    )[:32]


def _ensure_approved_proposal_unchanged(*, arguments, proposal):
    """Reject an approved execution when backend-resolved proposal drifted."""

    raw_event_id = str(
        (arguments or {}).get("approval_event_id") or ""
    ).strip()
    if not raw_event_id:
        return
    try:
        event_id = uuid.UUID(raw_event_id)
    except (TypeError, ValueError, AttributeError):
        return

    event = OperationsAuditEvent.objects.filter(
        pk=event_id,
        outcome=OperationsAuditEvent.Outcome.DRY_RUN,
    ).only("change_summary").first()
    summary = (
        event.change_summary
        if event and isinstance(event.change_summary, dict)
        else {}
    )
    expected = str(summary.get("proposal_digest") or "")
    if expected and expected != _proposal_digest(proposal):
        raise OperationsApprovalRequired(
            "The backend-resolved state changed after the approved dry-run. "
            "Run a fresh dry-run and obtain new approval."
        )



def _lead(organization, lead_id):
    lead = (
        Lead.objects.select_related("organization", "pipeline", "stage")
        .filter(pk=_uuid(lead_id, field="lead_id"), organization=organization)
        .first()
    )
    if lead is None:
        raise OperationsToolError("Lead not found in the active organization.")
    return lead


def _support_session(identity):
    if identity.role != ROLE_SUPERADMIN:
        return None
    return (
        OperationsSupportSession.objects.filter(
            token=identity.token,
            ended_at__isnull=True,
        )
        .select_related("organization", "actor")
        .order_by("-started_at")
        .first()
    )


def get_operations_context(*, identity, arguments):
    organization = _organization_for(identity, required=False)
    capabilities = sorted(
        effective_capabilities(
            role=identity.role,
            organization=organization,
        )
    )
    session = _support_session(identity)
    policy = policy_for(organization) if organization is not None else None
    return ToolExecution(
        data={
            "role": identity.role,
            "actor": {
                "id": str(identity.actor.id),
                "name": identity.actor.name,
            },
            "organization": (
                {
                    "id": str(organization.id),
                    "name": organization.name,
                    "active": organization.is_active,
                }
                if organization is not None
                else None
            ),
            "capabilities": capabilities,
            "organization_admin_external_ai_enabled": (
                policy.organization_admin_enabled if policy else None
            ),
            "superadmin_support_session": (
                {
                    "id": str(session.id),
                    "started_at": session.started_at.isoformat(),
                    "reason": sanitize_text(
                        session.reason,
                        limit=500,
                        redact_long=False,
                    ),
                }
                if session is not None
                else None
            ),
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization" if organization else "platform",
        target_id=str(organization.id) if organization else "",
        audit_summary={"context_present": organization is not None},
    )


def list_organizations(*, identity, arguments):
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError("Only SHVYA Superadmin can list organizations.")
    query = str((arguments or {}).get("query") or "").strip()
    try:
        limit = int((arguments or {}).get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    qs = Organization.objects.all().order_by("name")
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(users__email__icontains=query)).distinct()
    rows = []
    for organization in qs[:limit]:
        policy = policy_for(organization)
        active_support = OperationsSupportSession.objects.filter(
            organization=organization,
            ended_at__isnull=True,
            token__revoked_at__isnull=True,
            token__expires_at__gt=timezone.now(),
        ).exists()
        rows.append(
            {
                "id": str(organization.id),
                "name": organization.name,
                "active": organization.is_active,
                "organization_admin_external_ai_enabled": policy.organization_admin_enabled,
                "superadmin_support_active": active_support,
            }
        )
    return ToolExecution(
        data={"organizations": rows, "count": len(rows)},
        capability=CAP_ORGANIZATION_READ,
        target_type="platform",
        audit_summary={"result_count": len(rows)},
    )


def select_organization_context(*, identity, arguments):
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError(
            "Organization context selection is available only to SHVYA Superadmin."
        )
    reason = _reason(arguments, required=True)
    organization = Organization.objects.filter(
        pk=_uuid((arguments or {}).get("organization_id"), field="organization_id"),
        is_active=True,
    ).first()
    if organization is None:
        raise OperationsToolError("Active organization not found.")

    now = timezone.now()
    with transaction.atomic():
        token = identity.token.__class__.objects.select_for_update().get(pk=identity.token.pk)
        OperationsSupportSession.objects.filter(
            token=token,
            ended_at__isnull=True,
        ).update(ended_at=now, last_seen_at=now)
        token.active_organization = organization
        token.save(update_fields=["active_organization", "updated_at"])
        session = OperationsSupportSession.objects.create(
            token=token,
            actor=identity.actor,
            organization=organization,
            reason=reason,
        )
    identity.token.active_organization = organization
    return ToolExecution(
        data={
            "status": "selected",
            "organization": {"id": str(organization.id), "name": organization.name},
            "support_session_id": str(session.id),
            "organization_visibility": "SHVYA Support / Superadmin access is visible to organization admins while this context is active.",
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        reason=reason,
        audit_summary={"support_context": "started"},
    )


def clear_organization_context(*, identity, arguments):
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError(
            "Organization context clearing is available only to SHVYA Superadmin."
        )
    reason = _reason(arguments, required=True)
    previous = identity.token.active_organization
    now = timezone.now()
    with transaction.atomic():
        token = identity.token.__class__.objects.select_for_update().get(pk=identity.token.pk)
        OperationsSupportSession.objects.filter(
            token=token,
            ended_at__isnull=True,
        ).update(ended_at=now, last_seen_at=now)
        token.active_organization = None
        token.save(update_fields=["active_organization", "updated_at"])
    return ToolExecution(
        data={"status": "cleared"},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization" if previous else "platform",
        target_id=str(previous.id) if previous else "",
        reason=reason,
        audit_summary={"support_context": "ended"},
    )


def _reject_secret_like_content(value, *, field="configuration"):
    """Reject credential-like strings before persisting external-AI authored text."""

    if isinstance(value, dict):
        for key, item in value.items():
            _reject_secret_like_content(
                item,
                field=f"{field}.{key}",
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_like_content(
                item,
                field=f"{field}[{index}]",
            )
        return
    if not isinstance(value, str) or not value.strip():
        return

    redacted = sanitize_text(
        value,
        limit=max(len(value) + 32, 800),
        redact_long=False,
    )
    if redacted != value:
        raise OperationsPermissionError(
            f"{field} contains credential-like or secret material. "
            "Do not store secrets through SHVYA Operations MCP."
        )



def _attribute_value_compatible(*, field_type, options, value):
    """Return whether an existing stored CRM value remains valid for a definition."""

    if value is None or value == "" or value == [] or value == {}:
        return True
    text = str(value).strip()
    if not text:
        return True
    try:
        if field_type == AttributeDefinition.FieldType.NUMERIC:
            return Decimal(text).is_finite()
        if field_type == AttributeDefinition.FieldType.DATE:
            date.fromisoformat(text)
            return True
        if field_type == AttributeDefinition.FieldType.DATETIME:
            datetime.fromisoformat(text)
            return True
        if field_type == AttributeDefinition.FieldType.OPTION:
            return text in set(str(item) for item in (options or []))
    except (ValueError, TypeError, InvalidOperation):
        return False
    return True


def _incompatible_existing_attribute_value_count(
    *,
    organization,
    attribute,
    field_type,
    options,
):
    count = 0
    for attributes in (
        Lead.objects.filter(organization=organization)
        .values_list("attributes", flat=True)
        .iterator(chunk_size=500)
    ):
        if not isinstance(attributes, dict) or attribute.key not in attributes:
            continue
        if not _attribute_value_compatible(
            field_type=field_type,
            options=options,
            value=attributes.get(attribute.key),
        ):
            count += 1
    return count



def _sensitive_attribute_keys(organization):
    return {
        item.key
        for item in AttributeDefinition.objects.filter(
            organization=organization
        ).only("key", "name")
        if is_sensitive_attribute_definition(
            {"key": item.key, "name": item.name}
        )
    }


def _safe_workflow_config(rule, sensitive_keys):
    conditions = (
        deepcopy(rule.conditions)
        if isinstance(rule.conditions, dict)
        else {}
    )
    action = (
        deepcopy(rule.action)
        if isinstance(rule.action, dict)
        else {}
    )
    redacted = 0

    attribute_conditions = conditions.get("attributes")
    if isinstance(attribute_conditions, list):
        safe_conditions = []
        for item in attribute_conditions:
            if (
                isinstance(item, dict)
                and str(item.get("key") or "") in sensitive_keys
            ):
                safe_conditions.append(
                    {
                        "key": "[REDACTED_SENSITIVE_ATTRIBUTE]",
                        "match": item.get("match"),
                        "values": "[REDACTED]",
                    }
                )
                redacted += 1
            else:
                safe_conditions.append(item)
        conditions["attributes"] = safe_conditions

    if str(action.get("key") or "") in sensitive_keys:
        action["key"] = "[REDACTED_SENSITIVE_ATTRIBUTE]"
        if "value" in action:
            action["value"] = "[REDACTED]"
        redacted += 1

    if str(action.get("date_attribute") or "") in sensitive_keys:
        action["date_attribute"] = "[REDACTED_SENSITIVE_ATTRIBUTE]"
        redacted += 1

    return conditions, action, redacted



def get_organization_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )

    info = OrgInfo.objects.filter(organization=organization).first()
    pipelines = list(
        Pipeline.objects.filter(organization=organization)
        .prefetch_related("stages")
        .order_by("name")
    )
    attributes = list(
        AttributeDefinition.objects.filter(organization=organization)
        .order_by("display_order", "name")
    )
    sensitive_attribute_keys = _sensitive_attribute_keys(organization)
    visible_attributes = [
        item for item in attributes
        if item.key not in sensitive_attribute_keys
    ]
    playbook = str(getattr(info, "ai_playbook", "") or "")
    compiled = compile_qualification_requirements(qualification_questions(playbook))
    return ToolExecution(
        data={
            "organization": {
                "id": str(organization.id),
                "name": organization.name,
                "active": organization.is_active,
            },
            "ai": {
                "configured": info is not None,
                "about": str(getattr(info, "about", "") or "")[:4000],
                "bot_languages": str(getattr(info, "bot_languages", "") or "")[:500],
                "ai_enabled": bool(getattr(info, "ai_enabled", True)) if info else None,
                "bump_up_enabled": bool(getattr(info, "bump_up_enabled", True)) if info else None,
                "bump_up_count": int(getattr(info, "bump_up_count", 0)) if info else None,
                "playbook": playbook[:30000],
                "qualification": {
                    "mode": compiled.get("mode"),
                    "flow_version": compiled.get("flow_version"),
                    "requirements": compiled.get("requirements", []),
                },
            },
            "pipelines": [
                {
                    "id": str(pipeline.id),
                    "name": pipeline.name,
                    "active": pipeline.is_active,
                    "ai_enabled": pipeline.ai_enabled,
                    "stages": [
                        {
                            "id": str(stage.id),
                            "name": stage.name,
                            "active": stage.is_active,
                            "ai_on": stage.ai_on,
                            "display_order": stage.display_order,
                            "description": stage.description[:1000],
                        }
                        for stage in pipeline.stages.all()
                    ],
                }
                for pipeline in pipelines
            ],
            "attributes": [
                {
                    "id": str(item.id),
                    "key": item.key,
                    "name": item.name,
                    "field_type": item.field_type,
                    "description": item.description[:500],
                    "options": item.options,
                }
                for item in visible_attributes
            ],
            "automation": {
                "workflow_count": SmartTrigger.objects.filter(organization=organization).count(),
                "workflow_enabled_count": SmartTrigger.objects.filter(
                    organization=organization, enabled=True
                ).count(),
                "cadence_count": FollowupSequence.objects.filter(organization=organization).count(),
                "cadence_active_count": FollowupSequence.objects.filter(
                    organization=organization, is_active=True
                ).count(),
            },
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "pipelines": len(pipelines),
            "attributes": len(visible_attributes),
            "sensitive_attributes_redacted": len(sensitive_attribute_keys),
            "qualification_requirements": len(compiled.get("requirements", [])),
        },
    )


def get_automation_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )

    try:
        limit = int((arguments or {}).get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 100))

    workflows = list(
        SmartTrigger.objects.filter(organization=organization)
        .order_by("position", "created_at")[:limit]
    )
    sensitive_attribute_keys = _sensitive_attribute_keys(organization)
    workflow_rows = []
    workflow_redaction_count = 0
    for rule in workflows:
        safe_conditions, safe_action, redacted = _safe_workflow_config(
            rule,
            sensitive_attribute_keys,
        )
        workflow_redaction_count += redacted
        workflow_rows.append(
            {
                "id": str(rule.id),
                "name": rule.name,
                "enabled": rule.enabled,
                "position": rule.position,
                "trigger_type": rule.trigger_type,
                "conditions": safe_conditions,
                "action_type": rule.action_type,
                "action": safe_action,
                "updated_at": rule.updated_at.isoformat(),
            }
        )

    cadences = list(
        FollowupSequence.objects.filter(organization=organization)
        .select_related("whatsapp_account")
        .prefetch_related("steps__whatsapp_template")
        .order_by("-updated_at")[:limit]
    )
    for sequence in cadences:
        if (
            sequence.whatsapp_account_id
            and sequence.whatsapp_account.organization_id != organization.id
        ):
            raise OperationsPermissionError(
                "Cross-tenant Cadence account reference detected; no foreign data was returned."
            )
        for step in sequence.steps.all():
            if (
                step.whatsapp_template_id
                and step.whatsapp_template.organization_id != organization.id
            ):
                raise OperationsPermissionError(
                    "Cross-tenant Cadence template reference detected; no foreign data was returned."
                )

    return ToolExecution(
        data={
            "workflows": workflow_rows,
            "cadences": [
                {
                    "id": str(sequence.id),
                    "name": sequence.name,
                    "description": sequence.description,
                    "is_active": sequence.is_active,
                    "whatsapp_account": (
                        {
                            "id": str(sequence.whatsapp_account_id),
                            "connection_type": sequence.whatsapp_account.connection_type,
                            "business_name": sequence.whatsapp_account.business_name,
                            "display_phone_number": sequence.whatsapp_account.display_phone_number,
                            "status": sequence.whatsapp_account.status,
                            "is_active": sequence.whatsapp_account.is_active,
                        }
                        if sequence.whatsapp_account_id
                        else None
                    ),
                    "steps": [
                        {
                            "id": str(step.id),
                            "position": step.position,
                            "type": step.step_type,
                            "title": step.title,
                            "whatsapp_template": (
                                {
                                    "id": str(step.whatsapp_template_id),
                                    "name": step.whatsapp_template.name,
                                    "status": step.whatsapp_template.status,
                                }
                                if step.whatsapp_template_id
                                else None
                            ),
                            "email_subject": step.email_subject,
                            "email_body": step.email_body,
                            "reminder_text": step.reminder_text,
                            "schedule": {
                                "type": step.schedule_type,
                                "delay_value": step.delay_value,
                                "delay_unit": step.delay_unit,
                                "time": (
                                    step.specific_time.isoformat()
                                    if step.specific_time
                                    else None
                                ),
                                "weekday": step.specific_weekday,
                                "recurring_every": step.recurring_every,
                                "recurring_unit": step.recurring_unit,
                                "weekdays": list(step.recurring_weekdays or []),
                            },
                            "retry_count": step.retry_count,
                            "is_active": step.is_active,
                        }
                        for step in sequence.steps.all()
                    ],
                    "updated_at": sequence.updated_at.isoformat(),
                }
                for sequence in cadences
            ],
            "counts": {
                "workflows_returned": len(workflows),
                "cadences_returned": len(cadences),
                "sensitive_workflow_fields_redacted": workflow_redaction_count,
            },
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "workflows_returned": len(workflows),
            "cadences_returned": len(cadences),
            "sensitive_workflow_fields_redacted": workflow_redaction_count,
        },
    )


def _qualification_snapshot(lead):
    info = OrgInfo.objects.filter(organization=lead.organization).first()
    raw = str(getattr(info, "ai_playbook", "") or "")
    compiled = compile_qualification_requirements(qualification_questions(raw))
    requirements = requirements_for_lead(lead, compiled.get("requirements", []))
    state = state_for_lead(lead, requirements=requirements)
    return compiled, requirements, state


def _qualification_contract_snapshot(lead):
    compiled, requirements, state = _qualification_snapshot(lead)
    from apps.ai_engagement.services.playbook import criteria_for_lead
    from apps.ai_engagement.services.qualification_execution_contract import (
        _completion_target,
        _config,
    )

    config = _config(
        organization=lead.organization,
        requirements=requirements,
    )
    criteria = criteria_for_lead(
        lead=lead,
        state=state,
        requirements=requirements,
    )
    target = _completion_target(
        lead=lead,
        state=state,
        config=config,
    )
    return compiled, requirements, state, config, criteria, target


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
    lead = _lead(organization, (arguments or {}).get("lead_id"))
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
                Lead.objects.select_for_update()
                .select_related(
                    "organization",
                    "pipeline",
                    "stage",
                )
                .get(
                    pk=lead.pk,
                    organization=organization,
                )
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
    lead = _lead(organization, (arguments or {}).get("lead_id"))
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
    lead = _lead(organization, (arguments or {}).get("lead_id"))
    values = (arguments or {}).get("values")
    if not isinstance(values, dict) or not values:
        raise OperationsToolError("values must be a non-empty object keyed by CRM attribute key.")
    _reject_secret_like_content(values, field="lead_attributes")

    definitions = {
        item.key: item
        for item in AttributeDefinition.objects.filter(organization=organization)
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

    before = {
        key: (lead.attributes or {}).get(key)
        for key in values
    }
    proposal = {
        "lead_id": str(lead.id),
        "before": before,
        "after": values,
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
                Lead.objects.select_for_update()
                .select_related("organization", "pipeline", "stage")
                .get(
                    pk=lead.pk,
                    organization=organization,
                )
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

            locked_before = {
                key: (lead.attributes or {}).get(key)
                for key in values
            }
            locked_proposal = {
                "lead_id": str(lead.id),
                "before": locked_before,
                "after": values,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            update_lead_attribute_values(
                organization=organization,
                lead=lead,
                values=values,
            )
            lead.refresh_from_db(
                fields=["attributes", "updated_at"]
            )
            failed = [
                key
                for key, value in values.items()
                if str((lead.attributes or {}).get(key) or "")
                != str(value or "").strip()
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
            "updated_keys": sorted(values),
            "verification": "passed",
        },
        capability=CAP_LEAD_ATTRIBUTES_WRITE,
        target_type="lead",
        target_id=str(lead.id),
        reason=reason,
        audit_summary={
            "attribute_keys": sorted(values),
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
            limits = {"about": 12000, "bot_languages": 500, "ai_playbook": 50000}
            if len(text) > limits[key]:
                raise OperationsToolError(f"{key} is too large.")
            redacted = sanitize_text(
                text,
                limit=max(len(text) + 32, 800),
                redact_long=False,
            )
            if redacted != text:
                raise OperationsPermissionError(
                    f"{key} contains credential-like or secret material. "
                    "Do not store secrets in SHVYA AI configuration."
                )
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

    def window(start_at, end_at):
        created = Lead.objects.filter(
            organization=organization,
            created_at__gte=start_at,
            created_at__lt=end_at,
        )
        lead_count = created.count()

        source_rows = list(
            created.values("lead_source")
            .annotate(count=Count("id"))
            .order_by("-count", "lead_source")
        )
        pipeline_rows = list(
            created.values("pipeline_id", "pipeline__name")
            .annotate(count=Count("id"))
            .order_by("-count", "pipeline__name")
        )
        stage_rows = list(
            created.values(
                "pipeline__name",
                "stage_id",
                "stage__name",
            )
            .annotate(count=Count("id"))
            .order_by("-count", "pipeline__name", "stage__name")
        )

        qualified_activity = LeadActivity.objects.filter(
            organization=organization,
            topic__in=[
                LeadActivity.Topic.STAGE_CHANGED,
                LeadActivity.Topic.PIPELINE_CHANGED,
            ],
            created_at__gte=start_at,
            created_at__lt=end_at,
            lead__created_at__gte=start_at,
            lead__created_at__lt=end_at,
            new_stage_name__iexact="Qualified",
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
                .annotate(count=Count("lead_id", distinct=True))
            )
        }
        sources = [
            {
                "source": row["lead_source"],
                "lead_count": row["count"],
                "qualified_transitions": qualified_by_source.get(
                    row["lead_source"],
                    0,
                ),
                "qualified_transition_rate": (
                    round(
                        qualified_by_source.get(row["lead_source"], 0)
                        / row["count"],
                        4,
                    )
                    if row["count"]
                    else None
                ),
            }
            for row in source_rows
        ]

        messages = WhatsAppMessage.objects.filter(
            organization=organization,
            created_at__gte=start_at,
            created_at__lt=end_at,
        )
        outbound = messages.filter(
            direction=WhatsAppMessage.Direction.OUTBOUND,
        )
        outbound_status = {
            row["status"]: row["count"]
            for row in outbound.values("status").annotate(count=Count("id"))
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

        followups = FollowupExecution.objects.filter(
            organization=organization,
            scheduled_for__gte=start_at,
            scheduled_for__lt=end_at,
        )
        followup_status = {
            row["status"]: row["count"]
            for row in followups.values("status").annotate(count=Count("id"))
        }
        cadence_completed = LeadSequenceState.objects.filter(
            organization=organization,
            completed_at__gte=start_at,
            completed_at__lt=end_at,
        ).count()

        workflow_failures = TriggerRun.objects.filter(
            rule__organization=organization,
            status__in=["failed", "error"],
            created_at__gte=start_at,
            created_at__lt=end_at,
        ).count()
        ai_failures = HostedAutomationJob.objects.filter(
            organization=organization,
            status=HostedAutomationJob.Status.FAILED,
            created_at__gte=start_at,
            created_at__lt=end_at,
        ).count()

        campaign_deliveries = CampaignDelivery.objects.filter(
            campaign__organization=organization,
            created_at__gte=start_at,
            created_at__lt=end_at,
        )
        campaign_states = {
            row["state"]: row["count"]
            for row in campaign_deliveries.values("state").annotate(
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

        return {
            "lead_volume": lead_count,
            "qualified_transitions": qualified_count,
            "qualified_transition_rate": (
                round(qualified_count / lead_count, 4)
                if lead_count
                else None
            ),
            "source_mix": sources,
            "pipeline_mix": [
                {
                    "pipeline_id": str(row["pipeline_id"]),
                    "pipeline": row["pipeline__name"],
                    "lead_count": row["count"],
                }
                for row in pipeline_rows
            ],
            "stage_mix": [
                {
                    "pipeline": row["pipeline__name"],
                    "stage_id": str(row["stage_id"]),
                    "stage": row["stage__name"],
                    "lead_count": row["count"],
                }
                for row in stage_rows
            ],
            "messaging": {
                "outbound_total": outbound_total,
                "successful_outbound": successful_outbound,
                "failed_outbound": failed_outbound,
                "success_rate": (
                    round(successful_outbound / outbound_total, 4)
                    if outbound_total
                    else None
                ),
                "status_counts": outbound_status,
            },
            "followups": {
                "scheduled_executions": followups.count(),
                "status_counts": followup_status,
                "cadences_completed": cadence_completed,
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
                    round(campaign_delivered / campaign_total, 4)
                    if campaign_total
                    else None
                ),
                "reply_rate": (
                    round(campaign_replied / campaign_total, 4)
                    if campaign_total
                    else None
                ),
                "state_counts": campaign_states,
            },
        }

    current = window(current_start, now)
    previous = window(previous_start, current_start)

    observations = []
    comparable_metrics = (
        ("lead_volume", current["lead_volume"], previous["lead_volume"]),
        (
            "qualified_transition_rate",
            current["qualified_transition_rate"],
            previous["qualified_transition_rate"],
        ),
        (
            "message_success_rate",
            current["messaging"]["success_rate"],
            previous["messaging"]["success_rate"],
        ),
        (
            "workflow_failures",
            current["failures"]["workflow_failures"],
            previous["failures"]["workflow_failures"],
        ),
        (
            "hosted_ai_failures",
            current["failures"]["hosted_ai_failures"],
            previous["failures"]["hosted_ai_failures"],
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
    for metric, current_value, previous_value in comparable_metrics:
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
                "current": current["failures"]["workflow_failures"],
                "previous": previous["failures"]["workflow_failures"],
                "note": (
                    "Workflow failures increased during the same comparison "
                    "window. This is correlation, not proof of conversion causality."
                ),
            }
        )
    if (
        current["messaging"]["success_rate"] is not None
        and previous["messaging"]["success_rate"] is not None
        and current["messaging"]["success_rate"]
        < previous["messaging"]["success_rate"]
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": "message_delivery",
                "current": current["messaging"]["success_rate"],
                "previous": previous["messaging"]["success_rate"],
                "note": (
                    "Outbound WhatsApp success rate decreased while the compared "
                    "conversion proxy was measured. Causality requires lead-level "
                    "evidence."
                ),
            }
        )

    stale_cutoff = now - timedelta(days=7)
    stale_leads = Lead.objects.filter(
        organization=organization,
        stage_entered_at__lt=stale_cutoff,
    ).count()

    return ToolExecution(
        data={
            "comparison": {
                "days_per_period": days,
                "current_period": {
                    "start": current_start.isoformat(),
                    "end": now.isoformat(),
                    **current,
                },
                "previous_period": {
                    "start": previous_start.isoformat(),
                    "end": current_start.isoformat(),
                    **previous,
                },
            },
            "current_snapshot": {
                "leads_in_current_stage_for_7_plus_days": stale_leads,
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
                    "stage snapshot."
                ),
                (
                    "First-response-time and lost-reason aggregates are not included "
                    "because this tool does not have a canonical historical aggregate "
                    "for them; inspect specific conversations or add a dedicated "
                    "metric rather than guessing."
                ),
                "Likely-contributor labels are correlation, not proven causality.",
            ],
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "period_days": days,
            "observation_count": len(observations),
        },
    )


def get_operations_audit(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_AUDIT_READ,
    )
    try:
        limit = int((arguments or {}).get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 100))
    rows = OperationsAuditEvent.objects.filter(organization=organization).select_related("actor")
    if identity.role != ROLE_SUPERADMIN:
        # Organization admins can see Operations AI activity in their tenant,
        # including visible SHVYA Support actions, but never another tenant.
        rows = rows.filter(organization=organization)
    rows = rows.order_by("-created_at")[:limit]
    return ToolExecution(
        data={
            "events": [
                {
                    "id": str(item.id),
                    "actor": item.actor.name if item.actor else "Former user",
                    "role": item.role,
                    "tool": item.tool_name,
                    "capability": item.capability,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "reason": item.reason,
                    "outcome": item.outcome,
                    "change_summary": item.change_summary,
                    "error_code": item.error_code,
                    "created_at": item.created_at.isoformat(),
                }
                for item in rows
            ]
        },
        capability=CAP_AUDIT_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"result_count": len(rows)},
    )



def upsert_pipeline_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CRM_CONFIG_WRITE,
        tool_name="upsert_pipeline_configuration",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a pipeline configuration object.")
    _reject_secret_like_content(data, field="pipeline")

    pipeline_id = (arguments or {}).get("pipeline_id")
    pipeline = None
    if pipeline_id:
        pipeline = Pipeline.objects.filter(
            pk=_uuid(pipeline_id, field="pipeline_id"),
            organization=organization,
        ).first()
        if pipeline is None:
            raise OperationsToolError("Pipeline not found in the active organization.")

    name = str(data.get("name", pipeline.name if pipeline else "") or "").strip()
    description = str(
        data.get("description", pipeline.description if pipeline else "") or ""
    ).strip()
    if not name or len(name) > 150:
        raise OperationsToolError("Pipeline name is required and must be at most 150 characters.")
    if len(description) > 10000:
        raise OperationsToolError("Pipeline description is too large.")
    is_active = data.get("is_active", pipeline.is_active if pipeline else True)
    ai_enabled = data.get("ai_enabled", pipeline.ai_enabled if pipeline else True)
    if not isinstance(is_active, bool) or not isinstance(ai_enabled, bool):
        raise OperationsToolError("is_active and ai_enabled must be true or false.")
    if (
        pipeline is not None
        and pipeline.is_active
        and is_active is False
        and Lead.objects.filter(
            organization=organization,
            pipeline=pipeline,
        ).exists()
    ):
        raise OperationsPermissionError(
            "This pipeline still contains leads. Move those leads to another "
            "active pipeline before deactivating it."
        )
    duplicate = Pipeline.objects.filter(
        organization=organization,
        name__iexact=name,
    )
    if pipeline is not None:
        duplicate = duplicate.exclude(pk=pipeline.pk)
    if duplicate.exists():
        raise OperationsToolError("A pipeline with this name already exists.")

    before = (
        {
            "id": str(pipeline.id),
            "name": pipeline.name,
            "description": pipeline.description,
            "is_active": pipeline.is_active,
            "ai_enabled": pipeline.ai_enabled,
        }
        if pipeline
        else None
    )
    after = {
        "name": name,
        "description": description,
        "is_active": is_active,
        "ai_enabled": ai_enabled,
    }
    proposal = {
        "organization_id": str(organization.id),
        "pipeline_id": str(pipeline.id) if pipeline else None,
        "before": before,
        "after": after,
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
                "operation": "update" if pipeline else "create",
                "before": before,
                "after": after,
                "note": (
                    "A new pipeline automatically receives SHVYA's standard stages."
                    if pipeline is None
                    else None
                ),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CRM_CONFIG_WRITE,
                ),
                "reversible": pipeline is not None,
            },
            capability=CAP_CRM_CONFIG_WRITE,
            target_type="pipeline" if pipeline else "organization",
            target_id=str(pipeline.id) if pipeline else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_pipeline",
                "mode": "update" if pipeline else "create",
                "changed_fields": sorted(after),
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    creating_pipeline = pipeline is None
    with transaction.atomic():
        if pipeline is None:
            pipeline = Pipeline(
                organization=organization,
                name=name,
                description=description,
                is_active=is_active,
                ai_enabled=ai_enabled,
            )
        else:
            pipeline.name = name
            pipeline.description = description
            pipeline.is_active = is_active
            pipeline.ai_enabled = ai_enabled
        pipeline.full_clean()
        pipeline.save()

        if creating_pipeline:
            from apps.crm.models.signals import DEFAULT_PIPELINE_STAGES

            expected_stage_names = {
                str(item["name"])
                for item in DEFAULT_PIPELINE_STAGES
            }
            actual_stage_names = set(
                Stage.objects.filter(
                    pipeline=pipeline,
                    is_active=True,
                ).values_list("name", flat=True)
            )
            if not expected_stage_names.issubset(actual_stage_names):
                raise OperationsToolError(
                    "Pipeline creation did not produce SHVYA's required "
                    "standard stages; the transaction was rolled back."
                )

    pipeline.refresh_from_db()
    if (
        pipeline.name != name
        or pipeline.description != description
        or pipeline.is_active != is_active
        or pipeline.ai_enabled != ai_enabled
    ):
        raise OperationsToolError("Pipeline configuration verification failed.")
    return ToolExecution(
        data={
            "status": "FIXED",
            "pipeline": {
                "id": str(pipeline.id),
                "name": pipeline.name,
                "is_active": pipeline.is_active,
                "ai_enabled": pipeline.ai_enabled,
                "stage_count": pipeline.stages.count(),
            },
            "verification": "passed",
        },
        capability=CAP_CRM_CONFIG_WRITE,
        target_type="pipeline",
        target_id=str(pipeline.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_pipeline",
            "mode": "update" if before else "create",
            "changed_fields": sorted(after),
            "verification": "passed",
        },
    )


def upsert_stage_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CRM_CONFIG_WRITE,
        tool_name="upsert_stage_configuration",
        arguments=arguments,
    )
    pipeline = Pipeline.objects.filter(
        pk=_uuid((arguments or {}).get("pipeline_id"), field="pipeline_id"),
        organization=organization,
        is_active=True,
    ).first()
    if pipeline is None:
        raise OperationsToolError("Active pipeline not found in this organization.")

    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a stage configuration object.")
    _reject_secret_like_content(data, field="stage")
    stage_id = (arguments or {}).get("stage_id")
    stage = None
    if stage_id:
        stage = Stage.objects.filter(
            pk=_uuid(stage_id, field="stage_id"),
            pipeline=pipeline,
        ).first()
        if stage is None:
            raise OperationsToolError("Stage not found in the selected pipeline.")

    name = str(data.get("name", stage.name if stage else "") or "").strip()
    description = str(
        data.get("description", stage.description if stage else "") or ""
    ).strip()
    if not name or len(name) > 100:
        raise OperationsToolError("Stage name is required and must be at most 100 characters.")
    if len(description) > 1200:
        raise OperationsToolError("Stage description must be 1200 characters or fewer.")
    if stage is not None and stage.is_name_locked and name != stage.name:
        raise OperationsPermissionError("SHVYA protected stages cannot be renamed.")
    is_active = data.get("is_active", stage.is_active if stage else True)
    ai_on = data.get("ai_on", stage.ai_on if stage else True)
    if stage is not None and stage.is_system_locked and is_active is False:
        raise OperationsPermissionError("SHVYA protected stages cannot be deactivated.")
    if (
        stage is not None
        and is_active is False
        and Lead.objects.filter(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
        ).exists()
    ):
        raise OperationsPermissionError(
            "This stage still contains leads. Move those leads to another "
            "stage before deactivating it."
        )
    if not isinstance(is_active, bool) or not isinstance(ai_on, bool):
        raise OperationsToolError("is_active and ai_on must be true or false.")

    if "display_order" in data:
        try:
            display_order = int(data["display_order"])
        except (TypeError, ValueError) as exc:
            raise OperationsToolError("display_order must be a non-negative integer.") from exc
        if display_order < 0:
            raise OperationsToolError("display_order must be a non-negative integer.")
    elif stage is not None:
        display_order = stage.display_order
    else:
        display_order = (
            Stage.objects.filter(pipeline=pipeline).aggregate(value=Max("display_order"))["value"]
            or 0
        ) + 1

    duplicate_name = Stage.objects.filter(
        pipeline=pipeline,
        name__iexact=name,
        is_active=True,
    )
    duplicate_order = Stage.objects.filter(
        pipeline=pipeline,
        display_order=display_order,
    )
    if stage is not None:
        duplicate_name = duplicate_name.exclude(pk=stage.pk)
        duplicate_order = duplicate_order.exclude(pk=stage.pk)
    if is_active and duplicate_name.exists():
        raise OperationsToolError("An active stage with this name already exists.")
    if duplicate_order.exists():
        raise OperationsToolError("Another stage already uses this display order.")

    before = (
        {
            "id": str(stage.id),
            "name": stage.name,
            "description": stage.description,
            "display_order": stage.display_order,
            "is_active": stage.is_active,
            "ai_on": stage.ai_on,
        }
        if stage
        else None
    )
    after = {
        "name": name,
        "description": description,
        "display_order": display_order,
        "is_active": is_active,
        "ai_on": ai_on,
    }
    proposal = {
        "pipeline_id": str(pipeline.id),
        "stage_id": str(stage.id) if stage else None,
        "before": before,
        "after": after,
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
                "operation": "update" if stage else "create",
                "before": before,
                "after": after,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CRM_CONFIG_WRITE,
                ),
                "reversible": stage is not None,
            },
            capability=CAP_CRM_CONFIG_WRITE,
            target_type="stage" if stage else "pipeline",
            target_id=str(stage.id) if stage else str(pipeline.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_stage",
                "mode": "update" if stage else "create",
                "pipeline_id": str(pipeline.id),
                "changed_fields": sorted(after),
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    with transaction.atomic():
        if stage is None:
            stage = Stage(pipeline=pipeline)
        stage.name = name
        stage.description = description
        stage.display_order = display_order
        stage.is_active = is_active
        stage.ai_on = ai_on
        stage.full_clean()
        stage.save()
    stage.refresh_from_db()
    if (
        stage.name != name
        or stage.description != description
        or stage.display_order != display_order
        or stage.is_active != is_active
        or stage.ai_on != ai_on
    ):
        raise OperationsToolError("Stage configuration verification failed.")
    return ToolExecution(
        data={
            "status": "FIXED",
            "stage": {
                "id": str(stage.id),
                "pipeline_id": str(pipeline.id),
                "name": stage.name,
                "display_order": stage.display_order,
                "is_active": stage.is_active,
                "ai_on": stage.ai_on,
            },
            "verification": "passed",
        },
        capability=CAP_CRM_CONFIG_WRITE,
        target_type="stage",
        target_id=str(stage.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_stage",
            "mode": "update" if before else "create",
            "pipeline_id": str(pipeline.id),
            "changed_fields": sorted(after),
            "verification": "passed",
        },
    )


def upsert_attribute_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CRM_CONFIG_WRITE,
        tool_name="upsert_attribute_configuration",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be an attribute configuration object.")
    _reject_secret_like_content(data, field="attribute")
    attribute_id = (arguments or {}).get("attribute_id")
    attribute = None
    if attribute_id:
        attribute = AttributeDefinition.objects.filter(
            pk=_uuid(attribute_id, field="attribute_id"),
            organization=organization,
        ).first()
        if attribute is None:
            raise OperationsToolError("Attribute definition not found in this organization.")

    name = str(data.get("name", attribute.name if attribute else "") or "").strip()
    field_type = str(
        data.get(
            "field_type",
            attribute.field_type if attribute else AttributeDefinition.FieldType.TEXT,
        )
        or ""
    ).strip()
    description = str(
        data.get("description", attribute.description if attribute else "") or ""
    ).strip()
    options = data.get("options", list(attribute.options or []) if attribute else [])
    if not isinstance(options, list):
        raise OperationsToolError("Attribute options must be a list.")
    if field_type == AttributeDefinition.FieldType.OPTION:
        cleaned_options = []
        for item in options:
            value = str(item or "").strip()
            if value and value not in cleaned_options:
                cleaned_options.append(value)
        options = cleaned_options
    else:
        options = []
    if is_sensitive_attribute_definition({"key": name, "name": name}):
        raise OperationsPermissionError(
            "Credential-like or secret attribute definitions cannot be created through Operations MCP."
        )

    if attribute is not None and (
        field_type != attribute.field_type
        or list(options or []) != list(attribute.options or [])
    ):
        incompatible_count = _incompatible_existing_attribute_value_count(
            organization=organization,
            attribute=attribute,
            field_type=field_type,
            options=options,
        )
        if incompatible_count:
            raise OperationsPermissionError(
                "This attribute type/options change would make existing CRM "
                f"values invalid for {incompatible_count} lead(s). Update or "
                "clear those values first, then run a new dry-run."
            )

    before = (
        {
            "id": str(attribute.id),
            "key": attribute.key,
            "name": attribute.name,
            "field_type": attribute.field_type,
            "description": attribute.description,
            "options": attribute.options,
        }
        if attribute
        else None
    )
    after = {
        "name": name,
        "field_type": field_type,
        "description": description,
        "options": options,
    }
    proposal = {
        "organization_id": str(organization.id),
        "attribute_id": str(attribute.id) if attribute else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(
            arguments=arguments,
            proposal=proposal,
        )
    if dry_run:
        # Use a non-persisted instance for field-level validation while the
        # canonical service remains the write authority on execution.
        probe = AttributeDefinition(
            organization=organization,
            name=name,
            key=attribute.key if attribute else "operations_probe",
            field_type=field_type,
            description=description,
            options=options,
            display_order=attribute.display_order if attribute else 0,
        )
        try:
            probe.full_clean(validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            raise OperationsToolError("Attribute configuration validation failed.") from exc
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "operation": "update" if attribute else "create",
                "before": before,
                "after": after,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CRM_CONFIG_WRITE,
                ),
                "reversible": attribute is not None,
            },
            capability=CAP_CRM_CONFIG_WRITE,
            target_type="attribute" if attribute else "organization",
            target_id=str(attribute.id) if attribute else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_attribute",
                "mode": "update" if attribute else "create",
                "changed_fields": sorted(after),
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        if attribute is None:
            attribute = create_attribute_definition(
                organization=organization,
                name=name,
                field_type=field_type,
                description=description,
                options=options,
            )
        else:
            attribute = update_attribute_definition(
                organization=organization,
                attribute=attribute,
                name=name,
                field_type=field_type,
                description=description,
                options=options,
            )
    except ValidationError as exc:
        raise OperationsToolError("Attribute configuration validation failed.") from exc
    attribute.refresh_from_db()
    if (
        attribute.name != name
        or attribute.field_type != field_type
        or attribute.description != description
        or list(attribute.options or []) != list(options)
    ):
        raise OperationsToolError("Attribute configuration verification failed.")
    return ToolExecution(
        data={
            "status": "FIXED",
            "attribute": {
                "id": str(attribute.id),
                "key": attribute.key,
                "name": attribute.name,
                "field_type": attribute.field_type,
                "options": attribute.options,
            },
            "verification": "passed",
        },
        capability=CAP_CRM_CONFIG_WRITE,
        target_type="attribute",
        target_id=str(attribute.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_attribute",
            "mode": "update" if before else "create",
            "changed_fields": sorted(after),
            "verification": "passed",
        },
    )


def upsert_workflow_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AUTOMATION_CONFIG_WRITE,
        tool_name="upsert_workflow_configuration",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Workflow configuration object.")
    _reject_secret_like_content(data, field="workflow")
    try:
        clean = validate_workflow_rule(organization, data)
    except ValidationError as exc:
        raise OperationsToolError("Workflow validation failed.") from exc

    workflow_id = (arguments or {}).get("workflow_id")
    workflow = None
    if workflow_id:
        workflow = SmartTrigger.objects.filter(
            pk=_uuid(workflow_id, field="workflow_id"),
            organization=organization,
        ).first()
        if workflow is None:
            raise OperationsToolError("Workflow not found in this organization.")

    duplicate = SmartTrigger.objects.filter(
        organization=organization,
        fingerprint=clean["fingerprint"],
    )
    if workflow is not None:
        duplicate = duplicate.exclude(pk=workflow.pk)
    if duplicate.exists():
        raise OperationsToolError("An identical Workflow already exists.")

    workflow_before = (
        {
            "id": str(workflow.id),
            "name": workflow.name,
            "enabled": workflow.enabled,
            "position": workflow.position,
            "trigger_type": workflow.trigger_type,
            "conditions": workflow.conditions,
            "action_type": workflow.action_type,
            "action": workflow.action,
            "fingerprint": workflow.fingerprint,
        }
        if workflow is not None
        else None
    )
    proposed_position = (
        workflow.position
        if workflow is not None
        else (
            SmartTrigger.objects.filter(
                organization=organization
            ).aggregate(value=Max("position"))["value"]
            or 0
        ) + 1
    )
    workflow_after = {
        **clean,
        "position": proposed_position,
    }
    proposal = {
        "organization_id": str(organization.id),
        "workflow_id": str(workflow.id) if workflow else None,
        "before": workflow_before,
        "after": workflow_after,
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
                "operation": "update" if workflow else "create",
                "workflow": {
                    "name": clean["name"],
                    "trigger_type": clean["trigger_type"],
                    "action_type": clean["action_type"],
                    "enabled": clean["enabled"],
                    "scope_count": len((clean.get("conditions") or {}).get("scopes") or []),
                },
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AUTOMATION_CONFIG_WRITE,
                ),
                "reversible": workflow is not None,
            },
            capability=CAP_AUTOMATION_CONFIG_WRITE,
            target_type="workflow" if workflow else "organization",
            target_id=str(workflow.id) if workflow else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_workflow",
                "mode": "update" if workflow else "create",
                "trigger_type": clean["trigger_type"],
                "action_type": clean["action_type"],
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        if workflow is None:
            workflow = SmartTrigger(
                organization=organization,
                created_by=identity.actor,
                position=proposed_position,
            )
        for key, value in clean.items():
            setattr(workflow, key, value)
        workflow.save()
    workflow.refresh_from_db()
    if (
        workflow.fingerprint != clean["fingerprint"]
        or workflow.trigger_type != clean["trigger_type"]
        or workflow.action_type != clean["action_type"]
        or workflow.enabled != clean["enabled"]
    ):
        raise OperationsToolError("Workflow configuration verification failed.")
    return ToolExecution(
        data={
            "status": "FIXED",
            "workflow": {
                "id": str(workflow.id),
                "name": workflow.name,
                "trigger_type": workflow.trigger_type,
                "action_type": workflow.action_type,
                "enabled": workflow.enabled,
            },
            "verification": "passed",
        },
        capability=CAP_AUTOMATION_CONFIG_WRITE,
        target_type="workflow",
        target_id=str(workflow.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_workflow",
            "mode": "update" if workflow_id else "create",
            "trigger_type": workflow.trigger_type,
            "action_type": workflow.action_type,
            "verification": "passed",
        },
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
        capability=CAP_AUTOMATION_CONFIG_WRITE,
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
        sequence = FollowupSequence.objects.filter(
            pk=_uuid(sequence_id, field="cadence_id"),
            organization=organization,
        ).select_related("whatsapp_account").first()
        if sequence is None:
            raise OperationsToolError("Cadence not found in this organization.")

    name = str(data.get("name", sequence.name if sequence else "") or "").strip()
    description = str(
        data.get("description", sequence.description if sequence else "") or ""
    ).strip()
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

    account = sequence.whatsapp_account if sequence else None
    if sequence is not None:
        existing_provider = (
            "api"
            if account.connection_type == WhatsAppAccount.ConnectionType.API
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
            and str(requested_account_id).strip() != str(sequence.whatsapp_account_id)
        ):
            raise OperationsPermissionError(
                "Cadence WhatsApp sender cannot be changed on an existing "
                "Cadence through the canonical SHVYA service. Create a new "
                "Cadence for a different sender."
            )
        provider = existing_provider
    else:
        provider = str(data.get("provider") or "api").strip()

    if provider not in {"api", "hosted"}:
        raise OperationsToolError("Cadence provider must be api or hosted.")
    if sequence is None:
        account_id = data.get("whatsapp_account_id")
        if account_id:
            account = WhatsAppAccount.objects.filter(
                pk=_uuid(account_id, field="whatsapp_account_id"),
                organization=organization,
                is_active=True,
                status=WhatsAppAccount.Status.CONNECTED,
            ).first()
        if provider == "api" and account is None:
            raise OperationsToolError("An active connected WhatsApp API account is required.")
        if account is not None and provider == "api" and account.connection_type != WhatsAppAccount.ConnectionType.API:
            raise OperationsToolError("The selected account is not a WhatsApp API account.")
        if provider == "hosted":
            hosted_exists = WhatsAppAccount.objects.filter(
                organization=organization,
                connection_type=WhatsAppAccount.ConnectionType.coexisted,
                status=WhatsAppAccount.Status.CONNECTED,
                is_active=True,
            ).exists()
            if not hosted_exists:
                raise OperationsToolError(
                    "Connect at least one Hosted/Coexistence WhatsApp number before creating this Cadence."
                )

    cadence_before = (
        {
            "id": str(sequence.id),
            "name": sequence.name,
            "description": sequence.description,
            "provider": provider,
            "whatsapp_account_id": str(sequence.whatsapp_account_id),
        }
        if sequence is not None
        else None
    )
    cadence_after = {
        "name": name,
        "description": description,
        "provider": provider,
        "whatsapp_account_id": str(account.id) if account else None,
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
                    capability=CAP_AUTOMATION_CONFIG_WRITE,
                ),
                "reversible": sequence is not None,
            },
            capability=CAP_AUTOMATION_CONFIG_WRITE,
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
        if sequence is None:
            sequence = create_sequence(
                organization=organization,
                created_by=identity.actor,
                name=name,
                description=description,
                whatsapp_account=account,
                provider=provider,
            )
        else:
            sequence = update_sequence(
                sequence=sequence,
                name=name,
                description=description,
            )
    except FollowupError as exc:
        raise OperationsToolError(str(exc)) from exc
    sequence.refresh_from_db()
    if sequence.name != name or sequence.description != description:
        raise OperationsToolError("Cadence configuration verification failed.")
    return ToolExecution(
        data={
            "status": "FIXED",
            "cadence": {
                "id": str(sequence.id),
                "name": sequence.name,
                "step_count": sequence.steps.count(),
                "is_active": sequence.is_active,
            },
            "verification": "passed",
        },
        capability=CAP_AUTOMATION_CONFIG_WRITE,
        target_type="cadence",
        target_id=str(sequence.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_cadence",
            "mode": "update" if sequence_id else "create",
            "verification": "passed",
        },
    )


def add_cadence_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AUTOMATION_CONFIG_WRITE,
        tool_name="add_cadence_step",
        arguments=arguments,
    )
    sequence = FollowupSequence.objects.filter(
        pk=_uuid((arguments or {}).get("cadence_id"), field="cadence_id"),
        organization=organization,
        is_active=True,
    ).select_related("whatsapp_account").first()
    if sequence is None:
        raise OperationsToolError("Active Cadence not found in this organization.")
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Cadence step object.")
    _reject_secret_like_content(data, field="cadence_step")
    step_type = str(data.get("type") or "").strip().lower()
    if step_type not in {"whatsapp", "email", "reminder"}:
        raise OperationsToolError("Cadence step type must be whatsapp, email, or reminder.")
    schedule = _cadence_schedule(data)

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
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AUTOMATION_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AUTOMATION_CONFIG_WRITE,
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
                **schedule,
            )
        else:
            step = add_reminder_step(
                sequence=sequence,
                text=str(data.get("text") or ""),
                **schedule,
            )
    except FollowupError as exc:
        raise OperationsToolError(str(exc)) from exc
    step.refresh_from_db()
    verification_errors = []
    if step.sequence_id != sequence.id:
        verification_errors.append("sequence")
    if step.step_type != step_type:
        verification_errors.append("type")
    if step.position != step_proposal["next_position"]:
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
        expected_retry = int(data.get("retry_count", 0) or 0)
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
        if step.email_subject != str(data.get("subject") or "").strip():
            verification_errors.append("email_subject")
        if step.email_body != str(data.get("body") or "").strip():
            verification_errors.append("email_body")
    else:
        if step.reminder_text != str(data.get("text") or "").strip():
            verification_errors.append("reminder_text")

    if verification_errors:
        raise OperationsToolError(
            "Cadence step verification failed for: "
            + ", ".join(sorted(set(verification_errors)))
        )
    return ToolExecution(
        data={
            "status": "FIXED",
            "cadence_id": str(sequence.id),
            "step": {
                "id": str(step.id),
                "type": step.step_type,
                "position": step.position,
                "schedule_type": step.schedule_type,
            },
            "verification": "passed",
        },
        capability=CAP_AUTOMATION_CONFIG_WRITE,
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


def execute_operations_tool(*, name, identity, arguments):
    if name in DIAGNOSTIC_TOOL_NAMES:
        organization = _organization_for(identity)
        _require_operations_capability(
            identity=identity,
            organization=organization,
            capability=CAP_DIAGNOSTICS_READ,
        )
        try:
            data = execute_diagnostic_tool(
                name=name,
                organization=organization,
                arguments=arguments or {},
            )
        except DiagnosticToolError as exc:
            raise OperationsToolError(str(exc)) from exc
        target_id = str((arguments or {}).get("lead_id") or (arguments or {}).get("message_id") or "")
        return ToolExecution(
            data=data,
            capability=CAP_DIAGNOSTICS_READ,
            target_type="lead_or_message" if target_id else "organization",
            target_id=target_id or str(organization.id),
            audit_summary={"diagnostic_tool": name},
        )

    handlers = {
        "get_operations_context": get_operations_context,
        "list_organizations": list_organizations,
        "select_organization_context": select_organization_context,
        "clear_organization_context": clear_organization_context,
        "get_organization_configuration": get_organization_configuration,
        "get_automation_configuration": get_automation_configuration,
        "diagnose_lead_qualification": diagnose_lead_qualification,
        "move_lead_stage": move_lead_stage,
        "repair_qualification_stage": repair_qualification_stage,
        "update_lead_attributes": update_lead_attributes,
        "update_ai_configuration": update_ai_configuration,
        "get_conversion_analysis": get_conversion_analysis,
        "get_operations_audit": get_operations_audit,
        "upsert_pipeline_configuration": upsert_pipeline_configuration,
        "upsert_stage_configuration": upsert_stage_configuration,
        "upsert_attribute_configuration": upsert_attribute_configuration,
        "upsert_workflow_configuration": upsert_workflow_configuration,
        "upsert_cadence_configuration": upsert_cadence_configuration,
        "add_cadence_step": add_cadence_step,
    }
    handler = handlers.get(str(name or ""))
    if handler is None:
        raise OperationsToolError("Unknown SHVYA Operations tool.")
    return handler(identity=identity, arguments=arguments or {})
