# ruff: noqa: F401
"""Focused Operations MCP actions: audit actions."""

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
