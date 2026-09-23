# ruff: noqa: F401
"""Portable export and ETag support for Operations MCP configuration."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from apps.ai_engagement.models import FAQ, OrgInfo
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.followups.models import FollowupSequence, FollowupStep, LeadSequenceState
from apps.followups.touchpoint_models import TouchpointCategory, TouchpointReply
from apps.hosted_automation.models import HostedFollowupStepConfig
from apps.integrations.diagnostic_auth import sanitize_data
from apps.integrations.mcp_schema import (
    MCPInputValidationError,
    validate_mcp_arguments,
)
from apps.integrations.operations_models import (
    OperationsApprovalUse,
    OperationsAuditEvent,
    OperationsConfigurationPlan,
)
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_CONFIGURATION_PLAN_WRITE,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
    approval_required,
)
from apps.triggers.models import SmartTrigger
from services.channels.hosted_whatsapp_service import (
    get_pipeline_for_account,
    get_session_settings,
)
from services.triggers.rules import validate as validate_workflow_rule

from apps.integrations.operations_tools import (
    APPROVAL_RECEIPT_TTL,
    OperationsApprovalRequired,
    OperationsPermissionError,
    OperationsToolError,
    ToolExecution,
    _organization_for,
    _reason,
    _reject_secret_like_content,
    _require_operations_capability,
    _uuid,
    configuration_plan_execution,
)
from apps.integrations.operations.configuration.common import (
    ALLOWED_PLAN_TOOLS,
    EXPORT_SCHEMA_VERSION,
    INTERNAL_PLAN_TOOLS,
    PLAN_MAX_OPERATIONS,
    PLAN_MAX_TTL_MINUTES,
    PLAN_TOOL_CAPABILITIES,
    PLAN_TTL_MINUTES,
    PUBLIC_PLAN_TOOLS,
)

def _json_hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _account_ref(account) -> str:
    return (
        f"{account.connection_type}:"
        f"{str(account.display_phone_number or account.phone_number_id or '').strip()}"
    )


def _step_export(step):
    hosted = None
    if (
        step.step_type == FollowupStep.StepType.WHATSAPP
        and step.sequence.whatsapp_account.connection_type
        == WhatsAppAccount.ConnectionType.coexisted
    ):
        hosted = HostedFollowupStepConfig.objects.filter(step=step).first()
    return {
        "source_id": str(step.id),
        "position": step.position,
        "type": step.step_type,
        "title": step.title,
        "template_name": (
            step.whatsapp_template.name if step.whatsapp_template_id else None
        ),
        "hosted_body": hosted.body if hosted else None,
        "hosted_attachment": (
            {
                "name": hosted.attachment_original_name,
                "mime_type": hosted.attachment_mime_type,
                "size": hosted.attachment_size,
                "binary_included": False,
            }
            if hosted and hosted.attachment
            else None
        ),
        "email_subject": step.email_subject,
        "email_body": step.email_body,
        "reminder_text": step.reminder_text,
        "schedule": {
            "type": step.schedule_type,
            "delay_value": step.delay_value,
            "delay_unit": step.delay_unit,
            "time": step.specific_time.isoformat() if step.specific_time else None,
            "weekday": step.specific_weekday,
            "recurring_every": step.recurring_every,
            "recurring_unit": step.recurring_unit,
            "weekdays": list(step.recurring_weekdays or []),
        },
        "retry_count": step.retry_count,
        "active": step.is_active,
    }


def _portable_workflow(rule, *, pipelines, stages, sequences, accounts):
    conditions = deepcopy(rule.conditions if isinstance(rule.conditions, dict) else {})
    action = deepcopy(rule.action if isinstance(rule.action, dict) else {})

    portable_scopes = []
    for scope in conditions.get("scopes") or []:
        pipeline_id = str(scope.get("pipeline") or "")
        pipeline = pipelines.get(pipeline_id)
        if pipeline is None:
            continue
        portable_scopes.append(
            {
                "pipeline_name": pipeline.name,
                "stage_names": [
                    stages[str(stage_id)].name
                    for stage_id in scope.get("stages") or []
                    if str(stage_id) in stages
                ],
            }
        )
    conditions["scopes"] = portable_scopes

    if conditions.get("sequences") is not None:
        conditions["sequence_names"] = [
            sequences[str(item)].name
            for item in conditions.get("sequences") or []
            if str(item) in sequences
        ]
        conditions.pop("sequences", None)

    if rule.action_type == "move_stage":
        pipeline = pipelines.get(str(action.get("pipeline") or ""))
        stage = stages.get(str(action.get("stage") or ""))
        action = {
            "pipeline_name": pipeline.name if pipeline else "",
            "stage_name": stage.name if stage else "",
        }
    elif rule.action_type == "start_sequence":
        sequence = sequences.get(str(action.get("sequence") or ""))
        action["sequence_name"] = sequence.name if sequence else ""
        action.pop("sequence", None)
    elif rule.action_type == "message":
        account = accounts.get(str(action.get("account") or ""))
        action["account_ref"] = _account_ref(account) if account else ""
        action.pop("account", None)

    return {
        "source_id": str(rule.id),
        "name": rule.name,
        "enabled": rule.enabled,
        "trigger_type": rule.trigger_type,
        "conditions": conditions,
        "action_type": rule.action_type,
        "action": action,
        "position": rule.position,
    }


def _portable_configuration(organization):
    info = OrgInfo.objects.filter(organization=organization).first()
    pipelines_list = list(
        Pipeline.objects.filter(organization=organization).order_by("name", "id")
    )
    pipeline_map = {str(item.id): item for item in pipelines_list}
    stages_list = list(
        Stage.objects.filter(pipeline__organization=organization)
        .select_related("pipeline")
        .order_by("pipeline__name", "display_order", "name", "id")
    )
    stage_map = {str(item.id): item for item in stages_list}
    attributes = list(
        AttributeDefinition.objects.filter(
            organization=organization,
            is_active=True,
        )
        .order_by("display_order", "key", "id")
    )
    accounts_list = list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            is_active=True,
        )
        .defer("access_token")
        .order_by("connection_type", "display_phone_number", "id")
    )
    account_map = {str(item.id): item for item in accounts_list}
    sequences_list = list(
        FollowupSequence.objects.filter(organization=organization)
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
        .order_by("name", "id")
    )
    sequence_map = {str(item.id): item for item in sequences_list}
    workflows = list(
        SmartTrigger.objects.filter(
            organization=organization,
            is_active=True,
        )
        .order_by("position", "name", "id")
    )

    pipeline_rows = []
    for pipeline in pipelines_list:
        pipeline_rows.append(
            {
                "source_id": str(pipeline.id),
                "name": pipeline.name,
                "description": pipeline.description,
                "country_code": pipeline.country_code,
                "phone_number": pipeline.phone_number,
                "active": pipeline.is_active,
                "ai_enabled": pipeline.ai_enabled,
                "stages": [
                    {
                        "source_id": str(stage.id),
                        "name": stage.name,
                        "description": stage.description,
                        "display_order": stage.display_order,
                        "active": stage.is_active,
                        "ai_on": stage.ai_on,
                    }
                    for stage in stages_list
                    if stage.pipeline_id == pipeline.id
                ],
            }
        )

    cadence_rows = []
    for sequence in sequences_list:
        steps = list(
            sequence.steps.select_related(
                "sequence__whatsapp_account",
                "whatsapp_template",
            ).order_by("position", "created_at")
        )
        cadence_rows.append(
            {
                "source_id": str(sequence.id),
                "name": sequence.name,
                "description": sequence.description,
                "active": sequence.is_active,
                "provider": (
                    "api"
                    if sequence.whatsapp_account.connection_type
                    == WhatsAppAccount.ConnectionType.API
                    else "hosted"
                ),
                "account_ref": _account_ref(sequence.whatsapp_account),
                "steps": [_step_export(step) for step in steps],
            }
        )

    touchpoint_rows = []
    for category in (
        TouchpointCategory.objects.filter(organization=organization)
        .prefetch_related("replies")
        .order_by("name", "id")
    ):
        touchpoint_rows.append(
            {
                "source_id": str(category.id),
                "name": category.name,
                "replies": [
                    {
                        "source_id": str(reply.id),
                        "title": reply.title,
                        "body": reply.body,
                        "active": reply.is_active,
                    }
                    for reply in category.replies.all()
                ],
            }
        )

    messaging = []
    for account in accounts_list:
        pipeline = get_pipeline_for_account(account=account)
        messaging.append(
            {
                "account_ref": _account_ref(account),
                "connection_type": account.connection_type,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
                "pipeline_name": pipeline.name if pipeline else None,
                "settings": get_session_settings(account=account),
            }
        )

    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "organization": {
            "source_id": str(organization.id),
            "name": organization.name,
            "timezone": organization.timezone,
        },
        "ai": {
            "about": str(getattr(info, "about", "") or ""),
            "bot_languages": str(getattr(info, "bot_languages", "") or ""),
            "ai_playbook": str(getattr(info, "ai_playbook", "") or ""),
            "ai_enabled": bool(getattr(info, "ai_enabled", True)),
            "bump_up_enabled": bool(getattr(info, "bump_up_enabled", True)),
            "bump_up_count": int(getattr(info, "bump_up_count", 2)),
        },
        "pipelines": pipeline_rows,
        "attributes": [
            {
                "source_id": str(item.id),
                "key": item.key,
                "name": item.name,
                "field_type": item.field_type,
                "description": item.description,
                "options": list(item.options or []),
                "display_order": item.display_order,
            }
            for item in attributes
        ],
        "whatsapp_accounts": [
            {
                "source_id": str(item.id),
                "account_ref": _account_ref(item),
                "connection_type": item.connection_type,
                "display_phone_number": item.display_phone_number,
                "status": item.status,
            }
            for item in accounts_list
        ],
        "cadences": cadence_rows,
        "workflows": [
            _portable_workflow(
                rule,
                pipelines=pipeline_map,
                stages=stage_map,
                sequences=sequence_map,
                accounts=account_map,
            )
            for rule in workflows
        ],
        "touchpoints": touchpoint_rows,
        "faqs": [
            {
                "source_id": str(item.id),
                "question": item.question,
                "answer": item.answer,
                "active": item.is_active,
            }
            for item in FAQ.objects.filter(organization=organization)
            .order_by("id")
        ],
        "messaging": messaging,
        "excluded": {
            "credentials": True,
            "oauth_tokens": True,
            "provider_session_material": True,
            "knowledge_document_binaries": True,
            "lead_records": True,
            "message_history": True,
        },
    }


def configuration_etag(organization) -> str:
    return _json_hash(_portable_configuration(organization))


def _object_etag(value) -> str:
    return _json_hash(value)


def export_organization_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    configuration = _portable_configuration(organization)
    etag = _json_hash(configuration)
    return ToolExecution(
        data={
            "configuration_etag": etag,
            "configuration": configuration,
            "portable": True,
            "secret_material_included": False,
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "configuration_etag": etag,
            "pipeline_count": len(configuration["pipelines"]),
            "workflow_count": len(configuration["workflows"]),
            "cadence_count": len(configuration["cadences"]),
        },
    )
