# ruff: noqa: F401,E402
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


MESSAGING_AUTOMATION_SETTING_FIELDS = (
    "ai_auto_reply",
    "auto_lead_creation",
    "bump_up_messages",
    "bump_up_count",
    "auto_follow_up",
    "business_hours_start",
    "business_hours_end",
    "active_conversation_delay_value",
    "active_conversation_delay_unit",
)
MESSAGING_AUTOMATION_SETTING_KEYS = frozenset(
    MESSAGING_AUTOMATION_SETTING_FIELDS
)
ATTRIBUTE_COMPATIBILITY_SCAN_LIMIT = 5000
CONVERSION_BREAKDOWN_LIMIT = 100
LOST_REASON_BREAKDOWN_LIMIT = 20

_CONFIGURATION_PLAN_EXECUTION = ContextVar(
    "shvya_operations_configuration_plan_execution",
    default=False,
)


@contextmanager
def configuration_plan_execution():
    """Authorize nested member writes only inside one approved plan apply/rollback.

    This context is not user-controlled. Public MCP calls still pass through the
    ordinary write gate and one-use approval receipt checks.
    """

    token = _CONFIGURATION_PLAN_EXECUTION.set(True)
    try:
        yield
    finally:
        _CONFIGURATION_PLAN_EXECUTION.reset(token)


GENERIC_ACTION_REASONS = {
    "apply change",
    "fix issue",
    "fixing issue",
    "make change",
    "repair issue",
    "requested change",
    "resolve issue",
    "update config",
    "update configuration",
    "user asked me to",
    "user requested this",
}


DIAGNOSTIC_TOOL_NAMES = {
    "find_leads",
    "find_affected_leads",
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
        redact_long=True,
    )
    if safe != value:
        raise OperationsPermissionError(
            "Action reason contains credential-like or secret material. "
            "Use a non-secret operational reason."
        )
    normalized = " ".join(
        re.sub(r"[^a-z0-9]+", " ", value.casefold()).split()
    )
    if required and normalized in GENERIC_ACTION_REASONS:
        raise OperationsToolError(
            "A specific operational reason is required. Generic placeholders "
            "such as 'Fixing issue' or 'User asked me to' are not accepted."
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
        with transaction.atomic():
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

    if capability not in identity.granted_capabilities:
        raise OperationsPermissionError(
            f"Capability '{capability}' was not included in this OAuth grant. "
            "Fresh SHVYA authorization is required before the external AI can use it."
        )



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

    reason = _reason(arguments, required=True)

    if _CONFIGURATION_PLAN_EXECUTION.get():
        # apply_configuration_plan / rollback_configuration_plan owns the
        # outer approval, drift check, transaction, and audit event. Nested
        # member tools retain tenant/capability validation but do not require
        # independent approval receipts.
        return False, reason

    raw_dry_run = (arguments or {}).get("dry_run", True)
    raw_approved = (arguments or {}).get("approved", False)
    if not isinstance(raw_dry_run, bool):
        raise OperationsToolError("dry_run must be a JSON boolean.")
    if not isinstance(raw_approved, bool):
        raise OperationsToolError("approved must be a JSON boolean.")
    dry_run = raw_dry_run
    approved = raw_approved
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
    if not expected:
        raise OperationsApprovalRequired(
            "The approval receipt does not contain current backend proposal "
            "evidence. Run a fresh dry-run and obtain new approval."
        )
    if expected != _proposal_digest(proposal):
        raise OperationsApprovalRequired(
            "The backend-resolved state changed after the approved dry-run. "
            "Run a fresh dry-run and obtain new approval."
        )



def _tenant_safe_leads(organization):
    return (
        Lead.objects.filter(
            organization=organization,
            pipeline__organization=organization,
            stage__pipeline__organization=organization,
        )
        .filter(stage__pipeline_id=F("pipeline_id"))
    )


def _lead(organization, lead_id):
    lead = (
        _tenant_safe_leads(organization)
        .select_related("organization", "pipeline", "stage")
        .filter(pk=_uuid(lead_id, field="lead_id"))
        .first()
    )
    if lead is None:
        raise OperationsToolError("Lead not found in the active organization.")
    return lead


def _lead_for_write(*, organization, lead_id, dry_run, arguments):
    try:
        return _lead(organization, lead_id)
    except OperationsToolError as exc:
        approval_event_id = str(
            (arguments or {}).get("approval_event_id") or ""
        ).strip()
        if not dry_run and approval_event_id:
            raise OperationsApprovalRequired(
                "The lead or its tenant/pipeline/stage relationships changed "
                "after review. Run a fresh dry-run and obtain new approval."
            ) from exc
        raise


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
    policy_capabilities = set(
        effective_capabilities(
            role=identity.role,
            organization=organization,
        )
    )
    granted_capabilities = set(
        identity.granted_capabilities
    )
    capabilities = sorted(
        policy_capabilities
        & granted_capabilities
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
            "policy_capabilities": sorted(
                policy_capabilities
            ),
            "granted_capabilities": sorted(
                granted_capabilities
            ),
            "oauth_scopes": sorted(identity.scopes),
            "organization_admin_external_ai_enabled": (
                policy.organization_admin_enabled if policy else None
            ),
            "superadmin_support_context": (
                {
                    "id": str(session.id),
                    "started_at": session.started_at.isoformat(),
                    "last_seen_at": session.last_seen_at.isoformat(),
                    "recently_active": (
                        session.last_seen_at
                        >= timezone.now() - timedelta(minutes=15)
                    ),
                    "reason": sanitize_text(
                        session.reason,
                        limit=500,
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
    organization_count = qs.count()
    rows = []
    for organization in qs[:limit]:
        policy = policy_for(organization)
        support_context_open = OperationsSupportSession.objects.filter(
            organization=organization,
            ended_at__isnull=True,
            token__revoked_at__isnull=True,
            token__refresh_expires_at__gt=timezone.now(),
        ).exists()
        support_recently_active = visible_support_sessions(
            organization=organization,
        ).exists()
        rows.append(
            {
                "id": str(organization.id),
                "name": organization.name,
                "active": organization.is_active,
                "organization_admin_external_ai_enabled": policy.organization_admin_enabled,
                "superadmin_support_context_open": support_context_open,
                "superadmin_support_recently_active": support_recently_active,
                # Backward-compatible field now means visible/recent activity,
                # matching the customer-facing support-presence indicator.
                "superadmin_support_active": support_recently_active,
            }
        )
    return ToolExecution(
        data={
            "organizations": rows,
            "count": len(rows),
            "organization_count": organization_count,
            "organizations_returned": len(rows),
            "organizations_truncated": (
                organization_count > len(rows)
            ),
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="platform",
        audit_summary={
            "result_count": len(rows),
            "organization_count": organization_count,
            "truncated": organization_count > len(rows),
        },
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
    previous_session = None
    with transaction.atomic():
        token = (
            identity.token.__class__.objects.select_for_update()
            .get(pk=identity.token.pk)
        )
        previous_session = (
            OperationsSupportSession.objects.select_for_update()
            .select_related("organization")
            .filter(
                token=token,
                ended_at__isnull=True,
            )
            .order_by("-started_at")
            .first()
        )
        OperationsSupportSession.objects.filter(
            token=token,
            ended_at__isnull=True,
        ).update(
            ended_at=now,
            last_seen_at=now,
        )
        token.active_organization = organization
        token.save(
            update_fields=[
                "active_organization",
                "updated_at",
            ]
        )
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
            "support_context_id": str(session.id),
            "organization_visibility": "SHVYA Support / Superadmin access is visible to organization admins while this context is active.",
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        reason=reason,
        audit_summary={
            "support_context": "started",
            "_previous_support_session_id": (
                str(previous_session.id)
                if previous_session is not None
                else None
            ),
            "_previous_organization_id": (
                str(previous_session.organization_id)
                if previous_session is not None
                else None
            ),
        },
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



# Focused implementation modules are loaded behind a compatibility proxy.
# Existing callers/tests may patch symbols on this stable facade; before each
# delegated call we mirror those overrides into the focused implementation
# modules so the historical patch/injection contract remains intact.
from apps.integrations import operations_tool_read as _operations_read

# Read-layer helpers must be published before importing the action/config layers:
# those modules intentionally import these names from this stable facade.
_reject_secret_like_content = _operations_read._reject_secret_like_content
_attribute_value_compatible = _operations_read._attribute_value_compatible
_normalized_lead_attribute_values = _operations_read._normalized_lead_attribute_values
_validated_lead_attribute_values = _operations_read._validated_lead_attribute_values
_attribute_schema_snapshot = _operations_read._attribute_schema_snapshot
_incompatible_existing_attribute_value_count = (
    _operations_read._incompatible_existing_attribute_value_count
)
_sensitive_attribute_keys = _operations_read._sensitive_attribute_keys
_safe_workflow_config = _operations_read._safe_workflow_config
_assert_workflow_safe_attribute_references = (
    _operations_read._assert_workflow_safe_attribute_references
)
_workflow_reference_index = _operations_read._workflow_reference_index
_assert_workflow_tenant_references = _operations_read._assert_workflow_tenant_references
_safe_url_host = _operations_read._safe_url_host
_safe_knowledge_name = _operations_read._safe_knowledge_name
_knowledge_health = _operations_read._knowledge_health
_safe_full_config_text = _operations_read._safe_full_config_text
_qualification_snapshot = _operations_read._qualification_snapshot
_qualification_contract_snapshot = _operations_read._qualification_contract_snapshot
_messaging_account = _operations_read._messaging_account
_public_messaging_settings = _operations_read._public_messaging_settings
_safe_messaging_settings_row = _operations_read._safe_messaging_settings_row

from apps.integrations import operations_tool_actions as _operations_actions

_validate_operations_stage_move = _operations_actions._validate_operations_stage_move

from apps.integrations import operations_tool_config as _operations_config

_cadence_schedule = _operations_config._cadence_schedule

_OPERATIONS_DELEGATED_ENTRYPOINTS = frozenset(
    {
        "get_ai_configuration",
        "get_knowledge_health",
        "get_organization_configuration",
        "get_automation_configuration",
        "get_messaging_automation_settings",
        "update_messaging_automation_settings",
        "diagnose_lead_qualification",
        "move_lead_stage",
        "repair_qualification_stage",
        "update_lead_attributes",
        "update_ai_configuration",
        "get_conversion_analysis",
        "get_operations_audit",
        "upsert_pipeline_configuration",
        "upsert_stage_configuration",
        "upsert_attribute_configuration",
        "upsert_workflow_configuration",
        "upsert_cadence_configuration",
        "add_cadence_step",
    }
)


def _sync_facade_overrides():
    """Mirror facade-level patches into focused Operations implementation modules.

    The pre-split module was routinely patched in tests and diagnostics.  Moving
    functions must not silently change that contract: any symbol overridden on
    this facade is copied into matching implementation globals immediately before
    execution. Entrypoint functions themselves are excluded to avoid recursion.
    """

    facade = globals()
    for module in (_operations_read, _operations_actions, _operations_config):
        for name in tuple(module.__dict__):
            if name in _OPERATIONS_DELEGATED_ENTRYPOINTS:
                continue
            if name in facade:
                setattr(module, name, facade[name])


def _delegate_operations(module, name, *, identity, arguments):
    _sync_facade_overrides()
    return getattr(module, name)(identity=identity, arguments=arguments)


def get_ai_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_read, "get_ai_configuration", identity=identity, arguments=arguments
    )


def get_knowledge_health(*, identity, arguments):
    return _delegate_operations(
        _operations_read, "get_knowledge_health", identity=identity, arguments=arguments
    )


def get_organization_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_read,
        "get_organization_configuration",
        identity=identity,
        arguments=arguments,
    )


def get_automation_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_read,
        "get_automation_configuration",
        identity=identity,
        arguments=arguments,
    )


def get_messaging_automation_settings(*, identity, arguments):
    return _delegate_operations(
        _operations_read,
        "get_messaging_automation_settings",
        identity=identity,
        arguments=arguments,
    )


def update_messaging_automation_settings(*, identity, arguments):
    return _delegate_operations(
        _operations_actions,
        "update_messaging_automation_settings",
        identity=identity,
        arguments=arguments,
    )


def diagnose_lead_qualification(*, identity, arguments):
    return _delegate_operations(
        _operations_actions,
        "diagnose_lead_qualification",
        identity=identity,
        arguments=arguments,
    )


def move_lead_stage(*, identity, arguments):
    return _delegate_operations(
        _operations_actions, "move_lead_stage", identity=identity, arguments=arguments
    )


def repair_qualification_stage(*, identity, arguments):
    return _delegate_operations(
        _operations_actions,
        "repair_qualification_stage",
        identity=identity,
        arguments=arguments,
    )


def update_lead_attributes(*, identity, arguments):
    return _delegate_operations(
        _operations_actions,
        "update_lead_attributes",
        identity=identity,
        arguments=arguments,
    )


def update_ai_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_actions,
        "update_ai_configuration",
        identity=identity,
        arguments=arguments,
    )


def get_conversion_analysis(*, identity, arguments):
    return _delegate_operations(
        _operations_actions,
        "get_conversion_analysis",
        identity=identity,
        arguments=arguments,
    )


def get_operations_audit(*, identity, arguments):
    return _delegate_operations(
        _operations_actions, "get_operations_audit", identity=identity, arguments=arguments
    )


def upsert_pipeline_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_config,
        "upsert_pipeline_configuration",
        identity=identity,
        arguments=arguments,
    )


def upsert_stage_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_config,
        "upsert_stage_configuration",
        identity=identity,
        arguments=arguments,
    )


def upsert_attribute_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_config,
        "upsert_attribute_configuration",
        identity=identity,
        arguments=arguments,
    )


def upsert_workflow_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_config,
        "upsert_workflow_configuration",
        identity=identity,
        arguments=arguments,
    )


def upsert_cadence_configuration(*, identity, arguments):
    return _delegate_operations(
        _operations_config,
        "upsert_cadence_configuration",
        identity=identity,
        arguments=arguments,
    )


def add_cadence_step(*, identity, arguments):
    return _delegate_operations(
        _operations_config, "add_cadence_step", identity=identity, arguments=arguments
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
        "get_ai_configuration": get_ai_configuration,
        "get_knowledge_health": get_knowledge_health,
        "get_automation_configuration": get_automation_configuration,
        "get_messaging_automation_settings": get_messaging_automation_settings,
        "update_messaging_automation_settings": update_messaging_automation_settings,
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
        # Extended configuration tools are kept in lazily imported modules so
        # this core Operations boundary remains the single source of approval,
        # tenant, audit, and error semantics without creating import cycles.
        from apps.integrations.operations.registry import EXTENDED_HANDLERS

        handler = EXTENDED_HANDLERS.get(str(name or ""))
    if handler is None:
        from apps.integrations.operations_configuration_management import (
            CONFIGURATION_MANAGEMENT_HANDLERS,
        )

        handler = CONFIGURATION_MANAGEMENT_HANDLERS.get(str(name or ""))
    if handler is None:
        from apps.integrations.operations_lifecycle import LIFECYCLE_HANDLERS

        handler = LIFECYCLE_HANDLERS.get(str(name or ""))
    if handler is None:
        from apps.integrations.operations_superadmin_diagnostics import (
            SUPERADMIN_DIAGNOSTIC_HANDLERS,
        )

        handler = SUPERADMIN_DIAGNOSTIC_HANDLERS.get(str(name or ""))
    if handler is None:
        raise OperationsToolError("Unknown SHVYA Operations tool.")
    return handler(identity=identity, arguments=arguments or {})

