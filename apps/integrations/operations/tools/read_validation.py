# ruff: noqa: F401
"""Validation and tenant-safety helpers for Operations MCP reads."""

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
        redact_long=True,
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


def _normalized_lead_attribute_values(*, definitions, values):
    normalized = {}
    for key, value in values.items():
        definition = definitions[key]
        if isinstance(value, (dict, list, tuple, set)):
            raise OperationsToolError(
                f"CRM attribute '{definition.name}' requires a scalar value."
            )
        normalized[key] = (
            "" if value is None else str(value).strip()
        )
    return normalized


def _validated_lead_attribute_values(*, definitions, values):
    normalized = _normalized_lead_attribute_values(
        definitions=definitions,
        values=values,
    )
    for key, value in normalized.items():
        definition = definitions[key]
        if not _attribute_value_compatible(
            field_type=definition.field_type,
            options=definition.options,
            value=value,
        ):
            raise OperationsToolError(
                f"Value for CRM attribute '{definition.name}' does not match "
                "its configured type/options."
            )
    return normalized


def _attribute_schema_snapshot(*, definitions, keys):
    return {
        key: {
            "name": definitions[key].name,
            "field_type": definitions[key].field_type,
            "options": list(definitions[key].options or []),
        }
        for key in sorted(keys)
    }


def _incompatible_existing_attribute_value_count(
    *,
    organization,
    attribute,
    field_type,
    options,
):
    count = 0
    scanned = 0
    values = (
        _tenant_safe_leads(organization)
        .filter(**{"attributes__has_key": attribute.key})
        .order_by("id")
        .values_list("attributes", flat=True)[
            : ATTRIBUTE_COMPATIBILITY_SCAN_LIMIT + 1
        ]
    )
    for attributes in values.iterator(chunk_size=500):
        scanned += 1
        if scanned > ATTRIBUTE_COMPATIBILITY_SCAN_LIMIT:
            raise OperationsManualFixRequired(
                "This attribute type/options change requires checking more "
                f"than {ATTRIBUTE_COMPATIBILITY_SCAN_LIMIT} leads. Operations "
                "MCP will not perform an unbounded compatibility scan; use a "
                "dedicated CRM cleanup/migration workflow and then run a fresh "
                "dry-run."
            )
        if not isinstance(attributes, dict):
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
            organization=organization,
            is_active=True,
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


def _assert_workflow_safe_attribute_references(*, organization, clean):
    sensitive = _sensitive_attribute_keys(organization)
    if not sensitive:
        return

    referenced = set()
    conditions = clean.get("conditions") if isinstance(clean, dict) else {}
    action = clean.get("action") if isinstance(clean, dict) else {}
    conditions = conditions if isinstance(conditions, dict) else {}
    action = action if isinstance(action, dict) else {}

    for item in conditions.get("attributes") or []:
        if isinstance(item, dict):
            referenced.add(str(item.get("key") or ""))
    referenced.add(str(action.get("key") or ""))
    referenced.add(str(action.get("date_attribute") or ""))
    referenced.discard("")

    if referenced & sensitive:
        raise OperationsPermissionError(
            "Workflow configuration cannot read or write credential-like or "
            "sensitive CRM attributes through Operations MCP."
        )


def _workflow_reference_index(organization):
    stage_pairs = {
        (str(pipeline_id), str(stage_id))
        for pipeline_id, stage_id in Stage.objects.filter(
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        ).values_list("pipeline_id", "id")
    }
    return {
        "pipeline_ids": {
            str(item)
            for item in Pipeline.objects.filter(
                organization=organization,
                is_active=True,
            ).values_list("id", flat=True)
        },
        "stage_pairs": stage_pairs,
        "sequence_ids": {
            str(item)
            for item in (
                FollowupSequence.objects.filter(
                    organization=organization,
                    is_active=True,
                )
                .filter(
                    Q(
                        provider=FollowupSequence.Provider.HOSTED,
                        whatsapp_account__isnull=True,
                    )
                    | Q(
                        provider=FollowupSequence.Provider.HOSTED,
                        whatsapp_account__organization=organization,
                        whatsapp_account__connection_type=WhatsAppAccount.ConnectionType.coexisted,
                    )
                    | Q(
                        provider=FollowupSequence.Provider.API,
                        whatsapp_account__organization=organization,
                        whatsapp_account__connection_type=WhatsAppAccount.ConnectionType.API,
                    )
                )
                .values_list("id", flat=True)
            )
        },
        "account_ids": {
            str(item)
            for item in WhatsAppAccount.objects.filter(
                organization=organization,
                is_active=True,
                status=WhatsAppAccount.Status.CONNECTED,
                connection_type__in=[
                    WhatsAppAccount.ConnectionType.API,
                    WhatsAppAccount.ConnectionType.coexisted,
                ],
            ).values_list("id", flat=True)
        },
        "attribute_keys": {
            str(item)
            for item in AttributeDefinition.objects.filter(
                organization=organization,
                is_active=True,
            ).values_list("key", flat=True)
        },
    }


def _assert_workflow_tenant_references(rule, reference_index):
    def reject():
        raise OperationsPermissionError(
            "Workflow configuration contains a stale or cross-tenant "
            "resource reference. No referenced identifier was returned."
        )

    conditions = (
        rule.conditions
        if isinstance(rule.conditions, dict)
        else {}
    )
    action = (
        rule.action
        if isinstance(rule.action, dict)
        else {}
    )

    scopes = conditions.get("scopes", [])
    if not isinstance(scopes, list):
        reject()
    for scope in scopes:
        if not isinstance(scope, dict):
            reject()
        pipeline_id = str(
            scope.get("pipeline") or ""
        ).strip()
        if (
            pipeline_id
            and pipeline_id
            not in reference_index["pipeline_ids"]
        ):
            reject()
        stages = scope.get("stages", [])
        if not isinstance(stages, list):
            reject()
        for stage in stages:
            stage_id = str(stage or "").strip()
            if (
                stage_id
                and (
                    pipeline_id,
                    stage_id,
                )
                not in reference_index["stage_pairs"]
            ):
                reject()

    attribute_conditions = conditions.get("attributes", [])
    if not isinstance(attribute_conditions, list):
        reject()
    for condition in attribute_conditions:
        if not isinstance(condition, dict):
            reject()
        attribute_key = str(
            condition.get("key") or ""
        ).strip()
        if (
            attribute_key
            and attribute_key
            not in reference_index["attribute_keys"]
        ):
            reject()

    sequence_refs = conditions.get("sequences")
    if sequence_refs is not None:
        if not isinstance(sequence_refs, list):
            reject()
        for sequence_id in sequence_refs:
            value = str(sequence_id or "").strip()
            if (
                value
                and value
                not in reference_index["sequence_ids"]
            ):
                reject()

    if rule.action_type == "move_stage":
        pipeline_id = str(
            action.get("pipeline") or ""
        ).strip()
        stage_id = str(
            action.get("stage") or ""
        ).strip()
        if (
            pipeline_id
            and pipeline_id
            not in reference_index["pipeline_ids"]
        ):
            reject()
        if (
            stage_id
            and (
                pipeline_id,
                stage_id,
            )
            not in reference_index["stage_pairs"]
        ):
            reject()
    elif rule.action_type == "start_sequence":
        sequence_id = str(
            action.get("sequence") or ""
        ).strip()
        if (
            sequence_id
            and sequence_id
            not in reference_index["sequence_ids"]
        ):
            reject()
    elif rule.action_type == "message":
        account_id = str(
            action.get("account") or ""
        ).strip()
        if (
            account_id
            and account_id
            not in reference_index["account_ids"]
        ):
            reject()
    elif rule.action_type == "attribute":
        attribute_key = str(
            action.get("key") or ""
        ).strip()
        if (
            attribute_key
            and attribute_key
            not in reference_index["attribute_keys"]
        ):
            reject()
    elif rule.action_type == "reminder":
        date_attribute = str(
            action.get("date_attribute") or ""
        ).strip()
        if (
            date_attribute
            and date_attribute
            not in reference_index["attribute_keys"]
        ):
            reject()
