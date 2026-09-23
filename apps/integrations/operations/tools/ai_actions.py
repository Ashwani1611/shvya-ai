# ruff: noqa: F401
"""Focused Operations MCP actions: ai actions."""

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
                    text = validate_playbook(text)
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
