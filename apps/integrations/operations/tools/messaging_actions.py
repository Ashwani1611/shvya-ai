# ruff: noqa: F401
"""Focused Operations MCP actions: messaging actions."""

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
