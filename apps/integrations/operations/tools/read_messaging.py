# ruff: noqa: F401
"""Messaging automation read helpers and tool entrypoint."""

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
    _require_operations_capability,
    _tenant_safe_leads,
    OperationsToolError,
    OperationsPermissionError,
    OperationsManualFixRequired,
    ToolExecution,
    MESSAGING_AUTOMATION_SETTING_FIELDS,
    ATTRIBUTE_COMPATIBILITY_SCAN_LIMIT,
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


def _messaging_account(*, organization, account_id):
    account = (
        WhatsAppAccount.objects.select_related("organization")
        .defer("access_token")
        .filter(
            pk=_uuid(
                account_id,
                field="whatsapp_account_id",
            ),
            organization=organization,
            is_active=True,
        )
        .first()
    )
    if account is None:
        raise OperationsToolError(
            "Active WhatsApp account not found in this organization."
        )
    return account


def _public_messaging_settings(settings):
    settings = settings if isinstance(settings, dict) else {}
    return {
        key: settings.get(key)
        for key in MESSAGING_AUTOMATION_SETTING_FIELDS
        if key in settings
    }


def _safe_messaging_settings_row(account):
    pipeline = get_pipeline_for_account(
        account=account
    )
    return {
        "whatsapp_account_id": str(account.id),
        "connection_type": account.connection_type,
        "business_name": account.business_name,
        "display_phone_number": account.display_phone_number,
        "status": account.status,
        "pipeline": (
            {
                "id": str(pipeline.id),
                "name": pipeline.name,
            }
            if pipeline is not None
            else None
        ),
        "settings": _public_messaging_settings(
            get_session_settings(
                account=account
            )
        ),
    }


def get_messaging_automation_settings(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    arguments = arguments or {}
    account_id = str(
        arguments.get("whatsapp_account_id") or ""
    ).strip()
    if account_id:
        accounts = [
            _messaging_account(
                organization=organization,
                account_id=account_id,
            )
        ]
        account_count = 1
    else:
        account_qs = (
            WhatsAppAccount.objects.filter(
                organization=organization,
                is_active=True,
            )
            .defer("access_token")
            .order_by(
                "business_name",
                "display_phone_number",
                "id",
            )
        )
        account_count = account_qs.count()
        accounts = list(account_qs[:50])

    rows = [
        _safe_messaging_settings_row(account)
        for account in accounts
    ]
    return ToolExecution(
        data={
            "accounts": rows,
            "count": len(rows),
            "account_count": account_count,
            "accounts_returned": len(rows),
            "accounts_truncated": account_count > len(rows),
        },
        capability=CAP_ORGANIZATION_READ,
        target_type=(
            "whatsapp_account"
            if account_id
            else "organization"
        ),
        target_id=(
            account_id
            if account_id
            else str(organization.id)
        ),
        audit_summary={
            "result_count": len(rows),
            "account_count": account_count,
            "truncated": account_count > len(rows),
            "specific_account": bool(account_id),
        },
    )
