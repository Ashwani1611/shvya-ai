# ruff: noqa: F401
"""Read-only and configuration-inspection tools for SHVYA Operations MCP."""

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

def _operations_facade():
    """Resolve compatibility seams through the stable Operations facade."""
    from apps.integrations import operations_tools

    return operations_tools


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
    scan_limit = _operations_facade().ATTRIBUTE_COMPATIBILITY_SCAN_LIMIT
    values = (
        _tenant_safe_leads(organization)
        .filter(**{"attributes__has_key": attribute.key})
        .order_by("id")
        .values_list("attributes", flat=True)[
            : scan_limit + 1
        ]
    )
    for attributes in values.iterator(chunk_size=500):
        scanned += 1
        if scanned > scan_limit:
            raise OperationsManualFixRequired(
                "This attribute type/options change requires checking more "
                f"than {scan_limit} leads. Operations "
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
            for item in FollowupSequence.objects.filter(
                organization=organization,
                is_active=True,
                whatsapp_account__organization=organization,
            ).values_list("id", flat=True)
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


def _safe_url_host(value):
    try:
        parsed = urlparse(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    return (parsed.hostname or "").lower()[:255]


def _safe_knowledge_name(value, *, url=""):
    text = str(value or "").strip()
    parsed = None
    try:
        parsed = urlparse(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed and parsed.scheme in {"http", "https"} and parsed.hostname:
        return parsed.hostname.lower()[:255]
    if url and text == str(url).strip():
        return _safe_url_host(url)
    return sanitize_text(
        text,
        limit=255,
        redact_long=True,
    )


def _knowledge_health(*, organization, limit=50):
    limit = max(1, min(int(limit or 50), 100))
    source_qs = KnowledgeSource.objects.filter(
        organization=organization,
    )
    document_qs = Document.objects.filter(
        organization=organization,
    )
    sources = list(
        source_qs.order_by("-updated_at")[:limit]
    )
    documents = list(
        document_qs
        .annotate(
            has_processing_error=Case(
                When(processing_error="", then=Value(False)),
                default=Value(True),
                output_field=BooleanField(),
            ),
            has_file=Case(
                When(file="", then=Value(False)),
                default=Value(True),
                output_field=BooleanField(),
            ),
            share_instruction_present=Case(
                When(share_instruction="", then=Value(False)),
                default=Value(True),
                output_field=BooleanField(),
            ),
            chunk_count=Count("chunks", distinct=True),
            embedded_chunk_count=Count(
                "chunks",
                filter=Q(
                    chunks__embedding__isnull=False,
                    chunks__is_active=True,
                ),
                distinct=True,
            ),
            active_chunk_count=Count(
                "chunks",
                filter=Q(chunks__is_active=True),
                distinct=True,
            ),
        )
        .defer(
            "source_key",
            "file",
            "processing_error",
            "share_instruction",
        )
        .order_by("-updated_at", "-version")[:limit]
    )

    active_completed = document_qs.filter(
        is_active=True,
        processing_status=Document.ProcessingStatus.COMPLETED,
    ).count()
    failed = document_qs.filter(
        processing_status=Document.ProcessingStatus.FAILED,
    ).count()
    active_chunk_qs = Chunk.objects.filter(
        organization=organization,
        document__organization=organization,
        document__is_active=True,
        document__processing_status=Document.ProcessingStatus.COMPLETED,
        is_active=True,
    )
    active_chunks = active_chunk_qs.count()
    embedded_active_chunks = active_chunk_qs.filter(
        embedding__isnull=False,
    ).count()

    return {
        "summary": {
            "source_count": source_qs.count(),
            "document_count": document_qs.count(),
            "sources_returned": len(sources),
            "documents_returned": len(documents),
            "sources_truncated": source_qs.count() > len(sources),
            "documents_truncated": document_qs.count() > len(documents),
            "active_completed_documents": active_completed,
            "failed_documents": failed,
            "active_chunks": active_chunks,
            "embedded_active_chunks": embedded_active_chunks,
            "embedding_coverage": (
                round(
                    embedded_active_chunks / active_chunks,
                    4,
                )
                if active_chunks
                else None
            ),
        },
        "sources": [
            {
                "id": str(source.id),
                "source_type": source.source_type,
                "name": _safe_knowledge_name(
                    source.name,
                    url=source.url,
                ),
                "url_host": (
                    _safe_url_host(source.url)
                    if source.source_type
                    == KnowledgeSource.SourceType.URL
                    else ""
                ),
                "active": source.is_active,
                "created_at": source.created_at.isoformat(),
                "updated_at": source.updated_at.isoformat(),
            }
            for source in sources
        ],
        "documents": [
            {
                "id": str(document.id),
                "name": _safe_knowledge_name(
                    document.name,
                    url=document.source_url,
                ),
                "version": document.version,
                "active": document.is_active,
                "processing_status": document.processing_status,
                "has_processing_error": bool(
                    getattr(
                        document,
                        "has_processing_error",
                        False,
                    )
                ),
                "source_url_host": _safe_url_host(
                    document.source_url
                ),
                "has_file": bool(
                    getattr(document, "has_file", False)
                ),
                "share_instruction_present": bool(
                    getattr(
                        document,
                        "share_instruction_present",
                        False,
                    )
                ),
                "chunk_count": int(
                    getattr(document, "chunk_count", 0) or 0
                ),
                "active_chunk_count": int(
                    getattr(
                        document,
                        "active_chunk_count",
                        0,
                    )
                    or 0
                ),
                "embedded_chunk_count": int(
                    getattr(
                        document,
                        "embedded_chunk_count",
                        0,
                    )
                    or 0
                ),
                "created_at": document.created_at.isoformat(),
                "updated_at": document.updated_at.isoformat(),
            }
            for document in documents
        ],
    }


def _safe_full_config_text(value, *, max_chars):
    raw = str(value or "")
    safe_full = sanitize_text(
        raw,
        limit=max(len(raw) + 32, 800),
        redact_long=True,
    )
    return (
        safe_full[:max_chars],
        safe_full != raw,
        len(raw) > max_chars,
    )


def get_ai_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    info = OrgInfo.objects.filter(
        organization=organization
    ).first()
    if info is None:
        return ToolExecution(
            data={
                "configured": False,
                "organization_id": str(organization.id),
                "about": "",
                "bot_languages": "",
                "ai_playbook": "",
                "redactions": {
                    "about": False,
                    "bot_languages": False,
                    "ai_playbook": False,
                },
            },
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(organization.id),
            audit_summary={"configured": False},
        )

    (
        about,
        about_redacted,
        about_truncated,
    ) = _safe_full_config_text(
        info.about,
        max_chars=12000,
    )
    (
        languages,
        languages_redacted,
        languages_truncated,
    ) = _safe_full_config_text(
        info.bot_languages,
        max_chars=500,
    )
    (
        playbook,
        playbook_redacted,
        playbook_truncated,
    ) = _safe_full_config_text(
        info.ai_playbook,
        max_chars=100000,
    )
    compiled = compile_qualification_requirements(
        qualification_questions(
            str(info.ai_playbook or "")
        )
    )
    return ToolExecution(
        data={
            "configured": True,
            "organization_id": str(organization.id),
            "about": about,
            "about_length": len(str(info.about or "")),
            "about_truncated": about_truncated,
            "bot_languages": languages,
            "bot_languages_length": len(
                str(info.bot_languages or "")
            ),
            "bot_languages_truncated": languages_truncated,
            "ai_playbook": playbook,
            "ai_playbook_length": len(
                str(info.ai_playbook or "")
            ),
            "ai_playbook_truncated": playbook_truncated,
            "ai_enabled": bool(info.ai_enabled),
            "bump_up_enabled": bool(
                info.bump_up_enabled
            ),
            "bump_up_count": int(
                info.bump_up_count
            ),
            "redactions": {
                "about": about_redacted,
                "bot_languages": languages_redacted,
                "ai_playbook": playbook_redacted,
            },
            "qualification": {
                "mode": compiled.get("mode"),
                "flow_version": compiled.get(
                    "flow_version"
                ),
                "requirements": sanitize_data(
                    compiled.get(
                        "requirements",
                        [],
                    )
                ),
            },
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "configured": True,
            "about_length": len(
                str(info.about or "")
            ),
            "playbook_length": len(
                str(info.ai_playbook or "")
            ),
            "sensitive_text_redacted": any(
                (
                    about_redacted,
                    languages_redacted,
                    playbook_redacted,
                )
            ),
        },
    )


def get_knowledge_health(*, identity, arguments):
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
    health = _knowledge_health(
        organization=organization,
        limit=limit,
    )
    return ToolExecution(
        data=health,
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "sources_returned": health["summary"][
                "sources_returned"
            ],
            "documents_returned": health["summary"][
                "documents_returned"
            ],
            "failed_documents": health["summary"][
                "failed_documents"
            ],
        },
    )


def get_organization_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )

    info = OrgInfo.objects.filter(organization=organization).first()

    pipeline_qs = Pipeline.objects.filter(
        organization=organization
    ).order_by("name")
    pipeline_total = pipeline_qs.count()
    pipelines = list(pipeline_qs[:100])
    pipeline_ids = [pipeline.id for pipeline in pipelines]

    stage_qs = Stage.objects.filter(
        pipeline__organization=organization
    )
    stage_total = stage_qs.count()
    selected_stage_qs = stage_qs.filter(
        pipeline_id__in=pipeline_ids
    )
    stage_counts = {
        row["pipeline_id"]: row["count"]
        for row in selected_stage_qs.values(
            "pipeline_id"
        ).annotate(count=Count("id"))
    }
    stages = list(
        selected_stage_qs.order_by(
            "pipeline_id",
            "display_order",
            "name",
        )[:500]
    )
    stages_by_pipeline = {}
    for stage in stages:
        stages_by_pipeline.setdefault(
            stage.pipeline_id,
            [],
        ).append(stage)

    pipeline_rows = []
    for pipeline in pipelines:
        available_stages = stages_by_pipeline.get(
            pipeline.id,
            [],
        )
        returned_stages = available_stages[:100]
        stage_count = stage_counts.get(
            pipeline.id,
            0,
        )
        pipeline_rows.append(
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
                        "description_length": len(
                            stage.description or ""
                        ),
                        "description_truncated": len(
                            stage.description or ""
                        )
                        > 1000,
                    }
                    for stage in returned_stages
                ],
                "stage_count": stage_count,
                "stages_returned": len(returned_stages),
                "stages_truncated": (
                    stage_count > len(returned_stages)
                ),
            }
        )

    sensitive_attribute_keys = _sensitive_attribute_keys(
        organization
    )
    attribute_qs = (
        AttributeDefinition.objects.filter(
            organization=organization,
            is_active=True,
        )
        .exclude(key__in=sensitive_attribute_keys)
        .order_by("display_order", "name")
    )
    attribute_total = attribute_qs.count()
    visible_attributes = list(attribute_qs[:100])
    attribute_rows = []
    for item in visible_attributes:
        options = list(item.options or [])
        attribute_rows.append(
            {
                "id": str(item.id),
                "key": item.key,
                "name": item.name,
                "field_type": item.field_type,
                "description": item.description[:500],
                "description_length": len(
                    item.description or ""
                ),
                "description_truncated": len(
                    item.description or ""
                )
                > 500,
                "options": options[:100],
                "option_count": len(options),
                "options_truncated": len(options) > 100,
            }
        )

    stages_returned = sum(
        row["stages_returned"]
        for row in pipeline_rows
    )

    playbook = str(
        getattr(info, "ai_playbook", "") or ""
    )
    compiled = compile_qualification_requirements(
        qualification_questions(playbook)
    )
    requirements = list(
        compiled.get("requirements", []) or []
    )
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
                "about_length": len(
                    str(getattr(info, "about", "") or "")
                ),
                "about_truncated": len(
                    str(getattr(info, "about", "") or "")
                ) > 4000,
                "bot_languages": str(getattr(info, "bot_languages", "") or "")[:500],
                "ai_enabled": bool(getattr(info, "ai_enabled", True)) if info else None,
                "bump_up_enabled": bool(getattr(info, "bump_up_enabled", True)) if info else None,
                "bump_up_count": int(getattr(info, "bump_up_count", 0)) if info else None,
                "playbook": playbook[:30000],
                "playbook_length": len(playbook),
                "playbook_truncated": len(playbook) > 30000,
                "qualification": {
                    "mode": compiled.get("mode"),
                    "flow_version": compiled.get("flow_version"),
                    "requirements": requirements[:100],
                    "requirement_count": len(requirements),
                    "requirements_truncated": len(requirements) > 100,
                },
            },
            "pipelines": pipeline_rows,
            "attributes": attribute_rows,
            "counts": {
                "pipeline_count": pipeline_total,
                "pipelines_returned": len(pipeline_rows),
                "pipelines_truncated": (
                    pipeline_total > len(pipeline_rows)
                ),
                "stage_count": stage_total,
                "stages_returned": stages_returned,
                "stages_truncated": (
                    stage_total > stages_returned
                ),
                "attribute_count": attribute_total,
                "attributes_returned": len(attribute_rows),
                "attributes_truncated": (
                    attribute_total > len(attribute_rows)
                ),
                "sensitive_attributes_redacted": len(
                    sensitive_attribute_keys
                ),
            },
            "knowledge": _knowledge_health(
                organization=organization,
                limit=20,
            ),
            "automation": {
                "workflow_count": SmartTrigger.objects.filter(
                    organization=organization,
                    is_active=True,
                ).count(),
                "workflow_enabled_count": SmartTrigger.objects.filter(
                    organization=organization,
                    is_active=True,
                    enabled=True,
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
            "pipelines_returned": len(pipeline_rows),
            "pipelines_total": pipeline_total,
            "stages_returned": stages_returned,
            "stages_total": stage_total,
            "attributes_returned": len(attribute_rows),
            "attributes_total": attribute_total,
            "sensitive_attributes_redacted": len(
                sensitive_attribute_keys
            ),
            "qualification_requirements": len(
                requirements
            ),
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

    workflow_qs = SmartTrigger.objects.filter(
        organization=organization,
        is_active=True,
    )
    cadence_qs = FollowupSequence.objects.filter(
        organization=organization
    )
    workflow_total = workflow_qs.count()
    cadence_total = cadence_qs.count()

    workflows = list(
        workflow_qs.order_by(
            "position",
            "created_at",
        )[:limit]
    )
    sensitive_attribute_keys = _sensitive_attribute_keys(organization)
    workflow_reference_index = _workflow_reference_index(
        organization
    )
    workflow_rows = []
    workflow_redaction_count = 0
    for rule in workflows:
        _assert_workflow_tenant_references(
            rule,
            workflow_reference_index,
        )
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

    cadence_steps_qs = (
        FollowupStep.objects.select_related(
            "whatsapp_template"
        )
        .only(
            "id",
            "sequence_id",
            "position",
            "step_type",
            "title",
            "whatsapp_template_id",
            "email_subject",
            "email_body",
            "reminder_text",
            "schedule_type",
            "delay_value",
            "delay_unit",
            "specific_time",
            "specific_weekday",
            "recurring_every",
            "recurring_unit",
            "recurring_weekdays",
            "retry_count",
            "is_active",
            "created_at",
            "whatsapp_template__id",
            "whatsapp_template__organization_id",
            "whatsapp_template__account_id",
            "whatsapp_template__name",
            "whatsapp_template__status",
        )
        .order_by("position", "created_at")[:100]
    )
    cadences = list(
        cadence_qs
        .annotate(
            operations_step_count=Count(
                "steps",
                distinct=True,
            )
        )
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
        .prefetch_related(
            Prefetch(
                "steps",
                queryset=cadence_steps_qs,
                to_attr="operations_steps",
            )
        )
        .order_by("-updated_at")[:limit]
    )
    cadence_rows = []
    for sequence in cadences:
        if (
            sequence.whatsapp_account_id
            and sequence.whatsapp_account.organization_id != organization.id
        ):
            raise OperationsPermissionError(
                "Cross-tenant Cadence account reference detected; no foreign data was returned."
            )
        cadence_template_steps = FollowupStep.objects.filter(
            sequence=sequence,
            whatsapp_template__isnull=False,
        )
        if cadence_template_steps.exclude(
            whatsapp_template__organization=organization,
        ).exists():
            raise OperationsPermissionError(
                "Cross-tenant Cadence template reference detected; "
                "no foreign data was returned."
            )
        if cadence_template_steps.exclude(
            whatsapp_template__account_id=(
                sequence.whatsapp_account_id
            ),
        ).exists():
            raise OperationsPermissionError(
                "Cadence WhatsApp template does not belong to its "
                "configured sender account; no mismatched identifier "
                "was returned."
            )

        operations_steps = list(
            getattr(sequence, "operations_steps", [])
        )
        step_count = int(
            getattr(
                sequence,
                "operations_step_count",
                0,
            )
            or 0
        )
        cadence_rows.append(
            {
                "id": str(sequence.id),
                "name": sequence.name,
                "description": sequence.description,
                "is_active": sequence.is_active,
                "whatsapp_account": (
                    {
                        "id": str(sequence.whatsapp_account_id),
                        "connection_type": (
                            sequence.whatsapp_account.connection_type
                        ),
                        "business_name": (
                            sequence.whatsapp_account.business_name
                        ),
                        "display_phone_number": (
                            sequence.whatsapp_account.display_phone_number
                        ),
                        "status": sequence.whatsapp_account.status,
                        "is_active": (
                            sequence.whatsapp_account.is_active
                        ),
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
                                "id": str(
                                    step.whatsapp_template_id
                                ),
                                "name": (
                                    step.whatsapp_template.name
                                ),
                                "status": (
                                    step.whatsapp_template.status
                                ),
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
                            "weekdays": list(
                                step.recurring_weekdays or []
                            ),
                        },
                        "retry_count": step.retry_count,
                        "is_active": step.is_active,
                    }
                    for step in operations_steps
                ],
                "step_count": step_count,
                "steps_returned": len(operations_steps),
                "steps_truncated": (
                    step_count > len(operations_steps)
                ),
                "updated_at": sequence.updated_at.isoformat(),
            }
        )

    return ToolExecution(
        data={
            "workflows": workflow_rows,
            "cadences": cadence_rows,
            "counts": {
                "workflow_count": workflow_total,
                "cadence_count": cadence_total,
                "workflows_returned": len(workflows),
                "cadences_returned": len(cadences),
                "workflows_truncated": workflow_total > len(workflows),
                "cadences_truncated": cadence_total > len(cadences),
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


