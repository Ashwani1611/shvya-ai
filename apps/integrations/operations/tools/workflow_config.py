# ruff: noqa: F401
"""Focused Operations MCP configuration tools: workflow config."""

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

def upsert_workflow_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_WORKFLOW_CONFIG_WRITE,
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
    _assert_workflow_safe_attribute_references(
        organization=organization,
        clean=clean,
    )

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
            "is_active": workflow.is_active,
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
                organization=organization,
                is_active=True,
            ).aggregate(value=Max("position"))["value"]
            or 0
        ) + 1
    )
    workflow_after = {
        **clean,
        "is_active": True,
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
                    capability=CAP_WORKFLOW_CONFIG_WRITE,
                ),
                "reversible": workflow is not None,
            },
            capability=CAP_WORKFLOW_CONFIG_WRITE,
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

    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            try:
                clean_locked = validate_workflow_rule(
                    organization,
                    data,
                )
            except ValidationError as exc:
                raise OperationsApprovalRequired(
                    "Workflow references changed after review. "
                    "Run a fresh dry-run."
                ) from exc
            _assert_workflow_safe_attribute_references(
                organization=organization,
                clean=clean_locked,
            )

            if workflow is not None:
                workflow = (
                    SmartTrigger.objects.select_for_update()
                    .filter(
                        pk=workflow.pk,
                        organization=organization,
                    )
                    .first()
                )
                if workflow is None:
                    raise OperationsApprovalRequired(
                        "The Workflow changed or was removed after review. "
                        "Run a fresh dry-run."
                    )

            duplicate = SmartTrigger.objects.filter(
                organization=organization,
                fingerprint=clean_locked["fingerprint"],
            )
            if workflow is not None:
                duplicate = duplicate.exclude(pk=workflow.pk)
            if duplicate.exists():
                raise OperationsToolError(
                    "An identical Workflow already exists."
                )

            proposed_position = (
                workflow.position
                if workflow is not None
                else (
                    SmartTrigger.objects.filter(
                        organization=organization,
                        is_active=True,
                    ).aggregate(value=Max("position"))["value"]
                    or 0
                ) + 1
            )
            locked_before = (
                {
                    "id": str(workflow.id),
                    "name": workflow.name,
                    "enabled": workflow.enabled,
                    "is_active": workflow.is_active,
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
            locked_after = {
                **clean_locked,
                "is_active": True,
                "position": proposed_position,
            }
            locked_proposal = {
                "organization_id": str(organization.id),
                "workflow_id": (
                    str(workflow.id)
                    if workflow is not None
                    else None
                ),
                "before": locked_before,
                "after": locked_after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            if workflow is None:
                workflow = SmartTrigger(
                    organization=organization,
                    created_by=identity.actor,
                    position=proposed_position,
                )
            for key, value in clean_locked.items():
                setattr(workflow, key, value)
            workflow.is_active = True
            workflow.save()
            clean = clean_locked
            workflow.refresh_from_db()
            if (
                workflow.fingerprint != clean["fingerprint"]
                or workflow.trigger_type != clean["trigger_type"]
                or workflow.action_type != clean["action_type"]
                or workflow.enabled != clean["enabled"]
                or not workflow.is_active
            ):
                raise OperationsToolError(
                    "Workflow configuration verification failed."
                )
    except IntegrityError as exc:
        raise OperationsToolError(
            "Workflow configuration changed concurrently. "
            "Run a fresh dry-run."
        ) from exc
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
        capability=CAP_WORKFLOW_CONFIG_WRITE,
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
