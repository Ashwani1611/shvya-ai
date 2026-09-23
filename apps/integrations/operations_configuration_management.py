"""P1 configuration management for SHVYA Operations MCP.

The module provides organization configuration ETags, dependency/consistency
diagnostics, portable export/import plans, atomic multi-object plan execution,
and recoverable rollback for update-only plans. It intentionally reuses the
existing Operations tool handlers and their canonical domain services.
"""

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


PLAN_TTL_MINUTES = 30
PLAN_MAX_TTL_MINUTES = 24 * 60
PLAN_MAX_OPERATIONS = 200
EXPORT_SCHEMA_VERSION = 1

PUBLIC_PLAN_TOOLS = {
    "update_ai_configuration",
    "upsert_qualification_configuration",
    "upsert_pipeline_configuration",
    "upsert_stage_configuration",
    "upsert_attribute_configuration",
    "upsert_workflow_configuration",
    "upsert_cadence_configuration",
    "add_cadence_step",
    "add_hosted_whatsapp_step",
    "update_cadence_step",
    "reorder_cadence_steps",
    "upsert_touchpoint",
    "archive_touchpoint",
    "upsert_faq",
    "archive_faq",
    "update_messaging_automation_settings",
    "reorder_stages",
}
INTERNAL_PLAN_TOOLS = {
    "_ensure_stage_configuration",
}
ALLOWED_PLAN_TOOLS = PUBLIC_PLAN_TOOLS | INTERNAL_PLAN_TOOLS

PLAN_TOOL_CAPABILITIES = {
    "update_ai_configuration": CAP_AI_CONFIG_WRITE,
    "upsert_qualification_configuration": CAP_AI_CONFIG_WRITE,
    "upsert_pipeline_configuration": CAP_PIPELINE_CONFIG_WRITE,
    "upsert_stage_configuration": CAP_STAGE_CONFIG_WRITE,
    "_ensure_stage_configuration": CAP_STAGE_CONFIG_WRITE,
    "upsert_attribute_configuration": CAP_ATTRIBUTE_CONFIG_WRITE,
    "upsert_workflow_configuration": CAP_WORKFLOW_CONFIG_WRITE,
    "upsert_cadence_configuration": CAP_CADENCE_CONFIG_WRITE,
    "add_cadence_step": CAP_CADENCE_CONFIG_WRITE,
    "add_hosted_whatsapp_step": CAP_CADENCE_CONFIG_WRITE,
    "update_cadence_step": CAP_CADENCE_CONFIG_WRITE,
    "reorder_cadence_steps": CAP_CADENCE_CONFIG_WRITE,
    "upsert_touchpoint": CAP_CADENCE_CONFIG_WRITE,
    "archive_touchpoint": CAP_CADENCE_CONFIG_WRITE,
    "upsert_faq": CAP_AI_CONFIG_WRITE,
    "archive_faq": CAP_AI_CONFIG_WRITE,
    "update_messaging_automation_settings": CAP_MESSAGING_CONFIG_WRITE,
    "reorder_stages": CAP_STAGE_CONFIG_WRITE,
}


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


def _dependency_graph(organization):
    pipelines = list(
        Pipeline.objects.filter(organization=organization).order_by("name", "id")
    )
    stages = list(
        Stage.objects.filter(pipeline__organization=organization)
        .select_related("pipeline")
        .order_by("pipeline__name", "display_order", "id")
    )
    attributes = list(
        AttributeDefinition.objects.filter(
            organization=organization,
            is_active=True,
        )
        .order_by("display_order", "key")[:100]
    )
    workflows = list(
        SmartTrigger.objects.filter(
            organization=organization,
            is_active=True,
        )
        .order_by("position", "id")[:200]
    )
    cadences = list(
        FollowupSequence.objects.filter(organization=organization)
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
        .order_by("name")[:100]
    )

    lead_by_pipeline = {
        str(row["pipeline_id"]): row["count"]
        for row in Lead.objects.filter(organization=organization)
        .values("pipeline_id")
        .annotate(count=Count("id"))
    }
    lead_by_stage = {
        str(row["stage_id"]): row["count"]
        for row in Lead.objects.filter(organization=organization)
        .values("stage_id")
        .annotate(count=Count("id"))
    }
    cadence_state_counts = {
        str(row["sequence_id"]): row["count"]
        for row in LeadSequenceState.objects.filter(organization=organization)
        .values("sequence_id")
        .annotate(count=Count("id"))
    }

    nodes = []
    edges = []
    for pipeline in pipelines:
        pid = str(pipeline.id)
        nodes.append(
            {
                "type": "pipeline",
                "id": pid,
                "name": pipeline.name,
                "etag": _object_etag(
                    {
                        "name": pipeline.name,
                        "description": pipeline.description,
                        "active": pipeline.is_active,
                        "ai_enabled": pipeline.ai_enabled,
                        "country_code": pipeline.country_code,
                        "phone_number": pipeline.phone_number,
                    }
                ),
                "affected_leads": lead_by_pipeline.get(pid, 0),
            }
        )
    for stage in stages:
        sid = str(stage.id)
        nodes.append(
            {
                "type": "stage",
                "id": sid,
                "name": stage.name,
                "pipeline_id": str(stage.pipeline_id),
                "etag": _object_etag(
                    {
                        "name": stage.name,
                        "description": stage.description,
                        "order": stage.display_order,
                        "active": stage.is_active,
                        "ai_on": stage.ai_on,
                    }
                ),
                "affected_leads": lead_by_stage.get(sid, 0),
                "protected": stage.is_system_locked,
            }
        )
        edges.append(
            {
                "from": f"pipeline:{stage.pipeline_id}",
                "to": f"stage:{stage.id}",
                "relation": "contains",
            }
        )

    from apps.integrations.operations_extended_tools import _qualification_public_snapshot

    qualification = _qualification_public_snapshot(organization)
    completion = qualification.get("completion_stage")
    if completion:
        edges.append(
            {
                "from": "qualification:organization",
                "to": f"stage:{completion['id']}",
                "relation": "completion_target",
            }
        )

    mapping_keys = set()
    for values in (qualification.get("mappings") or {}).values():
        mapping_keys.update(str(value) for value in values)

    for attribute in attributes:
        lead_count = (
            Lead.objects.filter(
                organization=organization,
                **{"attributes__has_key": attribute.key},
            ).count()
        )
        nodes.append(
            {
                "type": "attribute",
                "id": str(attribute.id),
                "key": attribute.key,
                "name": attribute.name,
                "etag": _object_etag(
                    {
                        "key": attribute.key,
                        "name": attribute.name,
                        "field_type": attribute.field_type,
                        "description": attribute.description,
                        "options": list(attribute.options or []),
                    }
                ),
                "affected_leads": lead_count,
            }
        )
        if attribute.key in mapping_keys:
            edges.append(
                {
                    "from": "qualification:organization",
                    "to": f"attribute:{attribute.id}",
                    "relation": "maps_answer_to",
                }
            )

    for sequence in cadences:
        nodes.append(
            {
                "type": "cadence",
                "id": str(sequence.id),
                "name": sequence.name,
                "active": sequence.is_active,
                "affected_lead_states": cadence_state_counts.get(str(sequence.id), 0),
                "whatsapp_account_id": str(sequence.whatsapp_account_id),
            }
        )

    for rule in workflows:
        node_id = f"workflow:{rule.id}"
        nodes.append(
            {
                "type": "workflow",
                "id": str(rule.id),
                "name": rule.name,
                "enabled": rule.enabled,
                "trigger_type": rule.trigger_type,
                "action_type": rule.action_type,
                "etag": _object_etag(
                    {
                        "name": rule.name,
                        "enabled": rule.enabled,
                        "trigger_type": rule.trigger_type,
                        "conditions": rule.conditions,
                        "action_type": rule.action_type,
                        "action": rule.action,
                    }
                ),
            }
        )
        conditions = rule.conditions if isinstance(rule.conditions, dict) else {}
        action = rule.action if isinstance(rule.action, dict) else {}
        for scope in conditions.get("scopes") or []:
            for stage_id in scope.get("stages") or []:
                edges.append(
                    {
                        "from": node_id,
                        "to": f"stage:{stage_id}",
                        "relation": "trigger_scope",
                    }
                )
        for key in conditions.get("attributes") or []:
            attribute = next(
                (item for item in attributes if item.key == key.get("key")),
                None,
            )
            if attribute:
                edges.append(
                    {
                        "from": node_id,
                        "to": f"attribute:{attribute.id}",
                        "relation": "condition_attribute",
                    }
                )
        for sequence_id in conditions.get("sequences") or []:
            edges.append(
                {
                    "from": node_id,
                    "to": f"cadence:{sequence_id}",
                    "relation": "trigger_sequence",
                }
            )
        if rule.action_type == "move_stage" and action.get("stage"):
            edges.append(
                {
                    "from": node_id,
                    "to": f"stage:{action['stage']}",
                    "relation": "action_target",
                }
            )
        elif rule.action_type == "start_sequence" and action.get("sequence"):
            edges.append(
                {
                    "from": node_id,
                    "to": f"cadence:{action['sequence']}",
                    "relation": "action_target",
                }
            )
        elif rule.action_type == "attribute" and action.get("key"):
            attribute = next(
                (item for item in attributes if item.key == action.get("key")),
                None,
            )
            if attribute:
                edges.append(
                    {
                        "from": node_id,
                        "to": f"attribute:{attribute.id}",
                        "relation": "action_target",
                    }
                )

    return {
        "configuration_etag": configuration_etag(organization),
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "pipelines": len(pipelines),
            "stages": len(stages),
            "attributes": len(attributes),
            "workflows": len(workflows),
            "cadences": len(cadences),
        },
    }


def get_configuration_dependency_graph(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    graph = _dependency_graph(organization)
    object_type = str((arguments or {}).get("object_type") or "").strip()
    object_id = str((arguments or {}).get("object_id") or "").strip()
    if object_type and object_id:
        anchor = f"{object_type}:{object_id}"
        related = {anchor}
        for edge in graph["edges"]:
            if edge["from"] == anchor or edge["to"] == anchor:
                related.add(edge["from"])
                related.add(edge["to"])
        graph["nodes"] = [
            node
            for node in graph["nodes"]
            if f"{node['type']}:{node['id']}" in related
        ]
        graph["edges"] = [
            edge
            for edge in graph["edges"]
            if edge["from"] in related and edge["to"] in related
        ]
    return ToolExecution(
        data=graph,
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
            "filtered": bool(object_type and object_id),
        },
    )


def _workflow_cycles(workflows):
    graph = {}
    for rule in workflows:
        conditions = rule.conditions if isinstance(rule.conditions, dict) else {}
        action = rule.action if isinstance(rule.action, dict) else {}
        if rule.trigger_type == "stage_moved" and rule.action_type == "move_stage":
            target = str(action.get("stage") or "")
            for scope in conditions.get("scopes") or []:
                for source in scope.get("stages") or []:
                    source = str(source)
                    if source and target:
                        graph.setdefault(f"stage:{source}", set()).add(f"stage:{target}")
        if rule.trigger_type == "sequence_ended" and rule.action_type == "start_sequence":
            target = str(action.get("sequence") or "")
            for source in conditions.get("sequences") or []:
                source = str(source)
                if source and target:
                    graph.setdefault(f"cadence:{source}", set()).add(f"cadence:{target}")

    visiting = set()
    visited = set()
    cycles = []

    def walk(node, path):
        if node in visiting:
            index = path.index(node) if node in path else 0
            cycles.append(path[index:] + [node])
            return
        if node in visited:
            return
        visiting.add(node)
        path.append(node)
        for nxt in sorted(graph.get(node, ())):
            walk(nxt, path)
        path.pop()
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        walk(node, [])
    unique = []
    seen = set()
    for cycle in cycles:
        marker = tuple(cycle)
        if marker not in seen:
            unique.append(cycle)
            seen.add(marker)
    return unique[:50]


def _organization_validation(organization):
    errors = []
    warnings = []
    duplicates = []
    orphans = []

    def duplicate_groups(rows, *, object_type, parent_id=None):
        grouped = {}
        for row in rows:
            name = str(getattr(row, "name", "") or "").strip()
            key = name.casefold()
            if not key:
                continue
            grouped.setdefault(key, []).append(row)
        for key, items in grouped.items():
            if len(items) < 2:
                continue
            duplicates.append(
                {
                    "object_type": object_type,
                    "normalized_name": key,
                    "count": len(items),
                    "object_ids": [str(item.id) for item in items[:20]],
                    **({"parent_id": str(parent_id)} if parent_id else {}),
                }
            )

    from apps.integrations.operations_extended_tools import (
        _qualification_public_snapshot,
        _routing_snapshot,
    )

    routing = _routing_snapshot(organization)
    if not routing["valid"]:
        errors.append(
            {
                "code": "whatsapp_routing_invalid",
                "detail": routing,
            }
        )

    qualification = _qualification_public_snapshot(organization)
    for item in qualification.get("errors") or []:
        errors.append(
            {
                "code": str(item.get("code") or "qualification_configuration_error"),
                "detail": item,
            }
        )

    workflows = list(
        SmartTrigger.objects.filter(
            organization=organization,
            is_active=True,
        ).order_by("position", "id")
    )
    for rule in workflows:
        payload = {
            "name": rule.name,
            "enabled": rule.enabled,
            "trigger_type": rule.trigger_type,
            "conditions": rule.conditions,
            "action_type": rule.action_type,
            "action": rule.action,
        }
        try:
            validate_workflow_rule(organization, payload)
        except ValidationError as exc:
            detail = list(exc.messages)
            errors.append(
                {
                    "code": "invalid_workflow",
                    "workflow_id": str(rule.id),
                    "workflow_name": rule.name,
                    "detail": detail,
                }
            )
            orphans.append(
                {
                    "object_type": "workflow",
                    "object_id": str(rule.id),
                    "reason": "invalid_or_stale_reference",
                    "detail": detail,
                }
            )

    for cycle in _workflow_cycles(workflows):
        errors.append(
            {
                "code": "workflow_cycle",
                "path": cycle,
            }
        )

    for sequence in (
        FollowupSequence.objects.filter(organization=organization)
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
    ):
        positions = list(
            sequence.steps.order_by("position").values_list("position", flat=True)
        )
        if positions != list(range(1, len(positions) + 1)):
            errors.append(
                {
                    "code": "cadence_step_order_gap",
                    "cadence_id": str(sequence.id),
                    "cadence_name": sequence.name,
                    "positions": positions,
                }
            )
        if (
            sequence.whatsapp_account.connection_type
            == WhatsAppAccount.ConnectionType.coexisted
        ):
            missing = [
                str(step.id)
                for step in sequence.steps.filter(
                    step_type=FollowupStep.StepType.WHATSAPP,
                    is_active=True,
                )
                if not HostedFollowupStepConfig.objects.filter(step=step).exists()
            ]
            if missing:
                errors.append(
                    {
                        "code": "hosted_cadence_missing_content",
                        "cadence_id": str(sequence.id),
                        "step_ids": missing,
                    }
                )

    for pipeline in Pipeline.objects.filter(organization=organization):
        orders = list(
            pipeline.stages.order_by("display_order").values_list(
                "display_order", flat=True
            )
        )
        if len(orders) != len(set(orders)):
            errors.append(
                {
                    "code": "duplicate_stage_order",
                    "pipeline_id": str(pipeline.id),
                }
            )

    active_pipelines = list(
        Pipeline.objects.filter(
            organization=organization,
            is_active=True,
        ).only("id", "name")
    )
    duplicate_groups(active_pipelines, object_type="pipeline")
    duplicate_groups(
        list(
            AttributeDefinition.objects.filter(
                organization=organization,
                is_active=True,
            ).only("id", "name")
        ),
        object_type="attribute",
    )
    duplicate_groups(workflows, object_type="workflow")
    duplicate_groups(
        list(
            FollowupSequence.objects.filter(
                organization=organization,
                is_active=True,
            ).only("id", "name")
        ),
        object_type="cadence",
    )

    active_faqs = list(
        FAQ.objects.filter(
            organization=organization,
            is_active=True,
        ).only("id", "question")
    )
    faq_groups = {}
    for item in active_faqs:
        key = str(item.question or "").strip().casefold()
        if key:
            faq_groups.setdefault(key, []).append(item)
    for key, items in faq_groups.items():
        if len(items) > 1:
            duplicates.append(
                {
                    "object_type": "faq",
                    "normalized_question": key,
                    "count": len(items),
                    "object_ids": [str(item.id) for item in items[:20]],
                }
            )

    active_attribute_ids = {
        str(item)
        for item in AttributeDefinition.objects.filter(
            organization=organization,
            is_active=True,
        ).values_list("id", flat=True)
    }
    for pipeline in active_pipelines:
        stages = list(
            Stage.objects.filter(
                pipeline=pipeline,
                is_active=True,
            ).only("id", "name", "config")
        )
        duplicate_groups(
            stages,
            object_type="stage",
            parent_id=pipeline.id,
        )
        active_stage_names = {
            str(stage.name or "").strip().casefold()
            for stage in stages
        }
        for required_name in ("new leads", "qualified"):
            if required_name not in active_stage_names:
                orphan = {
                    "object_type": "pipeline",
                    "object_id": str(pipeline.id),
                    "reason": "required_system_stage_missing_or_inactive",
                    "required_stage": required_name,
                }
                orphans.append(orphan)
                errors.append(
                    {
                        "code": "required_system_stage_missing_or_inactive",
                        **orphan,
                    }
                )
        for stage in stages:
            required_ids = {
                str(value)
                for value in ((stage.config or {}).get("required_attribute_ids") or [])
            }
            missing = sorted(required_ids - active_attribute_ids)
            if missing:
                orphan = {
                    "object_type": "stage",
                    "object_id": str(stage.id),
                    "reason": "missing_or_inactive_required_attribute",
                    "attribute_ids": missing[:50],
                }
                orphans.append(orphan)
                errors.append(
                    {
                        "code": "stage_required_attribute_orphan",
                        **orphan,
                    }
                )

    for sequence in FollowupSequence.objects.filter(
        organization=organization,
        is_active=True,
    ).select_related("whatsapp_account").defer("whatsapp_account__access_token"):
        account = sequence.whatsapp_account
        if (
            not account.is_active
            or account.status != WhatsAppAccount.Status.CONNECTED
        ):
            orphan = {
                "object_type": "cadence",
                "object_id": str(sequence.id),
                "reason": "sender_inactive_or_disconnected",
            }
            orphans.append(orphan)
            errors.append(
                {
                    "code": "cadence_sender_unavailable",
                    **orphan,
                }
            )
        if not sequence.steps.filter(is_active=True).exists():
            warnings.append(
                {
                    "code": "active_cadence_has_no_active_steps",
                    "cadence_id": str(sequence.id),
                }
            )

    from apps.followups.touchpoint_models import TouchpointCategory

    empty_touchpoints = list(
        TouchpointCategory.objects.filter(
            organization=organization,
        )
        .annotate(
            active_reply_count=Count(
                "replies",
                filter=Q(replies__is_active=True),
            )
        )
        .filter(active_reply_count=0)
        .values_list("id", flat=True)[:50]
    )
    for category_id in empty_touchpoints:
        orphans.append(
            {
                "object_type": "touchpoint_category",
                "object_id": str(category_id),
                "reason": "no_active_saved_replies",
            }
        )
    if empty_touchpoints:
        warnings.append(
            {
                "code": "empty_touchpoint_categories",
                "count": len(empty_touchpoints),
            }
        )

    if duplicates:
        warnings.append(
            {
                "code": "duplicate_configuration_names",
                "count": len(duplicates),
            }
        )

    inactive_with_leads = list(
        Pipeline.objects.filter(
            organization=organization,
            is_active=False,
            leads__isnull=False,
        )
        .distinct()
        .values("id", "name")[:50]
    )
    if inactive_with_leads:
        warnings.append(
            {
                "code": "inactive_pipeline_contains_leads",
                "pipelines": [
                    {"id": str(item["id"]), "name": item["name"]}
                    for item in inactive_with_leads
                ],
            }
        )

    return {
        "valid": not errors,
        "configuration_etag": configuration_etag(organization),
        "errors": errors,
        "warnings": warnings,
        "duplicates": duplicates,
        "orphans": orphans,
        "counts": {
            "errors": len(errors),
            "warnings": len(warnings),
            "duplicates": len(duplicates),
            "orphans": len(orphans),
        },
    }


def validate_organization_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    result = _organization_validation(organization)
    return ToolExecution(
        data=result,
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "valid": result["valid"],
            "error_count": result["counts"]["errors"],
            "warning_count": result["counts"]["warnings"],
            "configuration_etag": result["configuration_etag"],
        },
    )


def reorder_stages(*, identity, arguments):
    from apps.integrations.operations_tools import (
        _ensure_approved_proposal_unchanged,
        _proposal_digest,
        _write_gate,
    )

    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_STAGE_CONFIG_WRITE,
        tool_name="reorder_stages",
        arguments=arguments,
    )
    pipeline = (
        Pipeline.objects.filter(
            pk=_uuid((arguments or {}).get("pipeline_id"), field="pipeline_id"),
            organization=organization,
        ).first()
    )
    if pipeline is None:
        raise OperationsToolError("Pipeline not found in this organization.")

    stage_ids = (arguments or {}).get("stage_ids")
    if not isinstance(stage_ids, list) or not all(isinstance(item, str) for item in stage_ids):
        raise OperationsToolError("stage_ids must be an ordered list of UUID strings.")
    existing = list(pipeline.stages.order_by("display_order", "name", "id"))
    existing_ids = [str(item.id) for item in existing]
    if len(stage_ids) != len(existing_ids) or set(stage_ids) != set(existing_ids):
        raise OperationsToolError(
            "stage_ids must contain every current pipeline stage exactly once."
        )

    proposal = {
        "pipeline_id": str(pipeline.id),
        "before": existing_ids,
        "after": stage_ids,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "pipeline_id": str(pipeline.id),
                "before": existing_ids,
                "after": stage_ids,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_STAGE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_STAGE_CONFIG_WRITE,
            target_type="pipeline",
            target_id=str(pipeline.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "reorder_stages",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    with transaction.atomic():
        locked = list(
            Stage.objects.select_for_update()
            .filter(pipeline=pipeline)
            .order_by("display_order", "name", "id")
        )
        locked_ids = [str(item.id) for item in locked]
        locked_proposal = {
            "pipeline_id": str(pipeline.id),
            "before": locked_ids,
            "after": stage_ids,
        }
        _ensure_approved_proposal_unchanged(
            arguments=arguments,
            proposal=locked_proposal,
        )
        if len(stage_ids) != len(locked) or set(stage_ids) != set(locked_ids):
            raise OperationsApprovalRequired(
                "Pipeline stages changed after review. Run a fresh dry-run."
            )
        by_id = {str(item.id): item for item in locked}
        offset = max([item.display_order for item in locked] + [0]) + len(locked) + 1000
        for index, item in enumerate(locked, start=1):
            item.display_order = offset + index
        Stage.objects.bulk_update(locked, ["display_order"])
        ordered = []
        for index, stage_id in enumerate(stage_ids, start=1):
            item = by_id[stage_id]
            item.display_order = index
            ordered.append(item)
        Stage.objects.bulk_update(ordered, ["display_order"])

    verified = [
        str(item)
        for item in pipeline.stages.order_by("display_order", "name", "id")
        .values_list("id", flat=True)
    ]
    if verified != stage_ids:
        raise OperationsToolError("Stage reorder verification failed.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "pipeline_id": str(pipeline.id),
            "stage_ids": stage_ids,
            "configuration_etag": configuration_etag(organization),
            "verification": "passed",
        },
        capability=CAP_STAGE_CONFIG_WRITE,
        target_type="pipeline",
        target_id=str(pipeline.id),
        reason=reason,
        audit_summary={"operation": "reorder_stages", "verification": "passed"},
    )


def _contains_ref(value):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return True
        return any(_contains_ref(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_ref(item) for item in value)
    return False


def _lookup_path(value, path):
    current = value
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise OperationsToolError(f"Plan reference path is unavailable: {path}.")
    return current


def _resolve_refs(value, refs):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            raw = str(value["$ref"] or "").strip()
            if "." not in raw:
                raise OperationsToolError("Plan reference must use ref_name.path.")
            ref_name, path = raw.split(".", 1)
            if ref_name not in refs:
                raise OperationsToolError(f"Plan reference is unresolved: {ref_name}.")
            return deepcopy(_lookup_path(refs[ref_name], path))
        return {key: _resolve_refs(item, refs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(item, refs) for item in value]
    return value


def _normalize_plan_operations(operations, *, allow_internal=False):
    if not isinstance(operations, list) or not operations or len(operations) > PLAN_MAX_OPERATIONS:
        raise OperationsToolError(
            f"operations must contain between 1 and {PLAN_MAX_OPERATIONS} items."
        )
    normalized = []
    refs = set()
    for index, item in enumerate(operations, start=1):
        if not isinstance(item, dict):
            raise OperationsToolError("Each plan operation must be an object.")
        tool = str(item.get("tool") or "").strip()
        allowed = ALLOWED_PLAN_TOOLS if allow_internal else PUBLIC_PLAN_TOOLS
        if tool not in allowed:
            raise OperationsPermissionError(
                f"Tool '{tool}' is not permitted inside configuration plans."
            )
        ref = str(item.get("ref") or f"operation_{index}").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ref):
            raise OperationsToolError("Plan operation ref is invalid.")
        if ref in refs:
            raise OperationsToolError(f"Duplicate plan operation ref: {ref}.")
        refs.add(ref)
        arguments = item.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise OperationsToolError("Plan operation arguments must be an object.")
        forbidden = {"dry_run", "approved", "approval_event_id"} & set(arguments)
        if forbidden:
            raise OperationsToolError(
                "Plan member operations cannot contain execution-control fields."
            )

        secret_check = deepcopy(arguments)
        if tool in {"add_hosted_whatsapp_step", "update_cadence_step"}:
            data = secret_check.get("data")
            if isinstance(data, dict):
                data.pop("attachment_base64", None)
        _reject_secret_like_content(
            secret_check,
            field=f"configuration_plan.{ref}",
        )

        normalized.append(
            {
                "ref": ref,
                "tool": tool,
                "arguments": deepcopy(arguments),
            }
        )
    return normalized


def _operation_is_reversible(tool, arguments):
    if tool == "upsert_pipeline_configuration":
        return bool(arguments.get("pipeline_id"))
    if tool in {"upsert_stage_configuration", "_ensure_stage_configuration"}:
        return bool(arguments.get("stage_id"))
    if tool == "upsert_attribute_configuration":
        return bool(arguments.get("attribute_id"))
    if tool == "upsert_workflow_configuration":
        return bool(arguments.get("workflow_id"))
    if tool == "upsert_cadence_configuration":
        return bool(arguments.get("cadence_id"))
    if tool in {"add_cadence_step", "add_hosted_whatsapp_step"}:
        return False
    if tool == "update_cadence_step":
        data = arguments.get("data") or {}
        return not bool(data.get("attachment_base64")) and not bool(
            data.get("remove_attachment", False)
        )
    if tool in {"upsert_touchpoint", "archive_touchpoint"}:
        return False
    if tool == "upsert_faq":
        return bool(arguments.get("faq_id"))
    return True


def _execute_plan_member(*, identity, tool, arguments):
    from apps.integrations.operations_tools import execute_operations_tool
    from apps.integrations.views.operations_mcp import TOOL_INPUT_SCHEMAS

    actual_tool = tool
    member_arguments = deepcopy(arguments)

    if tool == "_ensure_stage_configuration":
        organization = _organization_for(identity)
        pipeline_id = member_arguments.get("pipeline_id")
        name = str((member_arguments.get("data") or {}).get("name") or "").strip()
        stage = (
            Stage.objects.filter(
                pipeline_id=pipeline_id,
                pipeline__organization=organization,
                name__iexact=name,
            ).first()
        )
        if stage is not None:
            member_arguments["stage_id"] = str(stage.id)
        actual_tool = "upsert_stage_configuration"

    schema = TOOL_INPUT_SCHEMAS.get(actual_tool)
    if schema is None:
        raise OperationsPermissionError(
            f"Plan member tool '{actual_tool}' is not exposed by the Operations MCP."
        )
    try:
        validate_mcp_arguments(member_arguments, schema)
    except MCPInputValidationError as exc:
        raise OperationsToolError(
            f"Plan member '{actual_tool}' failed MCP schema validation: {exc}"
        ) from exc

    if actual_tool == "reorder_stages":
        return reorder_stages(identity=identity, arguments=member_arguments)
    return execute_operations_tool(
        name=actual_tool,
        identity=identity,
        arguments=member_arguments,
    )


def _dry_run_plan_member(*, identity, item, refs):
    arguments = _resolve_refs(item["arguments"], refs)
    arguments.update(
        {
            "dry_run": True,
            "approved": False,
            "reason": "Validate configuration plan member before human approval.",
        }
    )
    execution = _execute_plan_member(
        identity=identity,
        tool=item["tool"],
        arguments=arguments,
    )
    return execution, arguments


def create_configuration_plan(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_CONFIGURATION_PLAN_WRITE,
    )
    if "operations.write" not in identity.scopes:
        raise OperationsPermissionError(
            "This OAuth token does not include operations.write."
        )
    reason = _reason(arguments, required=True)
    operations = _normalize_plan_operations(
        (arguments or {}).get("operations"),
        allow_internal=bool((arguments or {}).get("_allow_internal", False)),
    )

    try:
        ttl_minutes = int((arguments or {}).get("ttl_minutes") or PLAN_TTL_MINUTES)
    except (TypeError, ValueError) as exc:
        raise OperationsToolError("ttl_minutes must be an integer.") from exc
    ttl_minutes = max(5, min(ttl_minutes, PLAN_MAX_TTL_MINUTES))
    idempotency_key = str((arguments or {}).get("idempotency_key") or "").strip()[:128]

    base_etag = configuration_etag(organization)
    refs = {}
    previews = []
    fully_reversible = True
    for item in operations:
        _require_operations_capability(
            identity=identity,
            organization=organization,
            capability=PLAN_TOOL_CAPABILITIES[item["tool"]],
        )
        reversible_hint = _operation_is_reversible(item["tool"], item["arguments"])
        fully_reversible = fully_reversible and reversible_hint
        if _contains_ref(item["arguments"]):
            previews.append(
                {
                    "ref": item["ref"],
                    "tool": item["tool"],
                    "validation": "deferred_until_atomic_apply",
                    "arguments": sanitize_data(item["arguments"]),
                    "reversible": reversible_hint,
                }
            )
            continue
        execution, resolved_args = _dry_run_plan_member(
            identity=identity,
            item=item,
            refs=refs,
        )
        if execution.outcome != OperationsAuditEvent.Outcome.DRY_RUN:
            raise OperationsToolError(
                f"Plan member {item['ref']} did not produce a dry-run."
            )
        preview = sanitize_data(execution.data)
        previews.append(
            {
                "ref": item["ref"],
                "tool": item["tool"],
                "validation": "validated",
                "preview": preview,
                "reversible": reversible_hint,
            }
        )
        refs[item["ref"]] = {
            "target_id": execution.target_id,
            "data": preview,
            "resolved_arguments": resolved_args,
        }

    plan_payload = {
        "organization_id": str(organization.id),
        "actor_id": str(identity.actor.id),
        "token_id": str(identity.token.id),
        "base_etag": base_etag,
        "operations": operations,
    }
    plan_hash = _json_hash(plan_payload)

    if idempotency_key:
        existing = OperationsConfigurationPlan.objects.filter(
            organization=organization,
            actor=identity.actor,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            if existing.plan_hash != plan_hash:
                raise OperationsToolError(
                    "This idempotency_key already belongs to a different configuration plan."
                )
            plan = existing
        else:
            plan = OperationsConfigurationPlan.objects.create(
                organization=organization,
                actor=identity.actor,
                token=identity.token,
                role=identity.role,
                idempotency_key=idempotency_key,
                reason=reason,
                operations=operations,
                base_etag=base_etag,
                plan_hash=plan_hash,
                reversible=fully_reversible,
                risk_summary={
                    "operation_count": len(operations),
                    "deferred_validation_count": sum(
                        1 for item in previews if item["validation"] != "validated"
                    ),
                    "atomic_apply": True,
                    "rollback_after_success": fully_reversible,
                },
                expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
            )
    else:
        plan = OperationsConfigurationPlan.objects.create(
            organization=organization,
            actor=identity.actor,
            token=identity.token,
            role=identity.role,
            reason=reason,
            operations=operations,
            base_etag=base_etag,
            plan_hash=plan_hash,
            reversible=fully_reversible,
            risk_summary={
                "operation_count": len(operations),
                "deferred_validation_count": sum(
                    1 for item in previews if item["validation"] != "validated"
                ),
                "atomic_apply": True,
                "rollback_after_success": fully_reversible,
            },
            expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
        )

    needs_approval = approval_required(
        role=identity.role,
        organization=organization,
        capability=CAP_CONFIGURATION_PLAN_WRITE,
    )
    return ToolExecution(
        data={
            "status": "DRY_RUN",
            "plan_id": str(plan.id),
            "plan_digest": plan.plan_hash[:32],
            "base_configuration_etag": plan.base_etag,
            "expires_at": plan.expires_at.isoformat(),
            "operations": previews,
            "risk": plan.risk_summary,
            "approval_required": needs_approval,
            "reversible_after_success": plan.reversible,
            "atomic_apply": True,
        },
        capability=CAP_CONFIGURATION_PLAN_WRITE,
        target_type="configuration_plan",
        target_id=str(plan.id),
        reason=reason,
        outcome=OperationsAuditEvent.Outcome.DRY_RUN,
        audit_summary={
            "operation": "create_configuration_plan",
            "plan_digest": plan.plan_hash[:32],
            "operation_count": len(operations),
            "base_etag": plan.base_etag,
            "reversible": plan.reversible,
        },
    )


def _consume_plan_approval(*, identity, organization, plan, arguments):
    needs_approval = approval_required(
        role=identity.role,
        organization=organization,
        capability=CAP_CONFIGURATION_PLAN_WRITE,
    )
    if not needs_approval:
        return None
    if (arguments or {}).get("approved") is not True:
        raise OperationsApprovalRequired(
            "Approve the complete configuration plan before applying it."
        )
    raw_event_id = str((arguments or {}).get("approval_event_id") or "").strip()
    try:
        event_id = uuid.UUID(raw_event_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise OperationsApprovalRequired(
            "approval_event_id must be the UUID returned by create_configuration_plan or import_organization_configuration."
        ) from exc
    event = OperationsAuditEvent.objects.filter(
        pk=event_id,
        actor=identity.actor,
        role=identity.role,
        organization=organization,
        tool_name__in=[
            "create_configuration_plan",
            "import_organization_configuration",
        ],
        capability=CAP_CONFIGURATION_PLAN_WRITE,
        target_type="configuration_plan",
        target_id=str(plan.id),
        outcome=OperationsAuditEvent.Outcome.DRY_RUN,
        created_at__gte=timezone.now() - APPROVAL_RECEIPT_TTL,
    ).first()
    summary = event.change_summary if event and isinstance(event.change_summary, dict) else {}
    if event is None or str(summary.get("plan_digest") or "") != plan.plan_hash[:32]:
        raise OperationsApprovalRequired(
            "The plan approval receipt is missing, expired, actor/tenant mismatched, "
            "or does not match this exact plan hash."
        )
    try:
        with transaction.atomic():
            OperationsApprovalUse.objects.create(approval_event=event)
    except IntegrityError as exc:
        raise OperationsApprovalRequired(
            "This plan approval receipt has already been consumed."
        ) from exc
    return event


def _snapshot_step_for_inverse(step):
    hosted = HostedFollowupStepConfig.objects.filter(step=step).first()
    data = {
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
        "is_active": step.is_active,
    }
    if hosted is not None:
        data.update({"title": step.title, "body": hosted.body})
    elif step.step_type == FollowupStep.StepType.WHATSAPP:
        data["template_id"] = str(step.whatsapp_template_id)
    elif step.step_type == FollowupStep.StepType.EMAIL:
        data.update(
            {
                "title": step.title,
                "subject": step.email_subject,
                "body": step.email_body,
            }
        )
    else:
        data["text"] = step.reminder_text
    return data


def _capture_inverse(*, organization, tool, arguments):
    if tool == "update_ai_configuration":
        info = OrgInfo.objects.filter(organization=organization).first() or OrgInfo(
            organization=organization
        )
        changes = arguments.get("changes") or {}
        return {
            "tool": "update_ai_configuration",
            "arguments": {
                "changes": {key: getattr(info, key) for key in changes},
            },
        }
    if tool == "upsert_qualification_configuration":
        info = OrgInfo.objects.filter(organization=organization).first()
        return {
            "tool": "update_ai_configuration",
            "arguments": {
                "changes": {"ai_playbook": str(getattr(info, "ai_playbook", "") or "")},
            },
        }
    if tool == "upsert_pipeline_configuration" and arguments.get("pipeline_id"):
        obj = Pipeline.objects.get(
            pk=arguments["pipeline_id"], organization=organization
        )
        return {
            "tool": tool,
            "arguments": {
                "pipeline_id": str(obj.id),
                "data": {
                    "name": obj.name,
                    "description": obj.description,
                    "is_active": obj.is_active,
                    "ai_enabled": obj.ai_enabled,
                },
            },
        }
    if tool in {"upsert_stage_configuration", "_ensure_stage_configuration"} and arguments.get("stage_id"):
        obj = Stage.objects.select_related("pipeline").get(
            pk=arguments["stage_id"], pipeline__organization=organization
        )
        return {
            "tool": "upsert_stage_configuration",
            "arguments": {
                "pipeline_id": str(obj.pipeline_id),
                "stage_id": str(obj.id),
                "data": {
                    "name": obj.name,
                    "description": obj.description,
                    "display_order": obj.display_order,
                    "is_active": obj.is_active,
                    "ai_on": obj.ai_on,
                },
            },
        }
    if tool == "upsert_attribute_configuration" and arguments.get("attribute_id"):
        obj = AttributeDefinition.objects.get(
            pk=arguments["attribute_id"], organization=organization
        )
        return {
            "tool": tool,
            "arguments": {
                "attribute_id": str(obj.id),
                "data": {
                    "name": obj.name,
                    "field_type": obj.field_type,
                    "description": obj.description,
                    "options": list(obj.options or []),
                },
            },
        }
    if tool == "upsert_workflow_configuration" and arguments.get("workflow_id"):
        obj = SmartTrigger.objects.get(
            pk=arguments["workflow_id"], organization=organization
        )
        return {
            "tool": tool,
            "arguments": {
                "workflow_id": str(obj.id),
                "data": {
                    "name": obj.name,
                    "enabled": obj.enabled,
                    "trigger_type": obj.trigger_type,
                    "conditions": deepcopy(obj.conditions),
                    "action_type": obj.action_type,
                    "action": deepcopy(obj.action),
                },
            },
        }
    if tool == "upsert_cadence_configuration" and arguments.get("cadence_id"):
        obj = FollowupSequence.objects.select_related("whatsapp_account").get(
            pk=arguments["cadence_id"], organization=organization
        )
        return {
            "tool": tool,
            "arguments": {
                "cadence_id": str(obj.id),
                "data": {
                    "name": obj.name,
                    "description": obj.description,
                    "provider": (
                        "api"
                        if obj.whatsapp_account.connection_type
                        == WhatsAppAccount.ConnectionType.API
                        else "hosted"
                    ),
                    "whatsapp_account_id": str(obj.whatsapp_account_id),
                },
            },
        }
    if tool == "update_cadence_step":
        step = FollowupStep.objects.select_related(
            "sequence__whatsapp_account", "whatsapp_template"
        ).get(
            pk=arguments["step_id"],
            sequence_id=arguments["cadence_id"],
            sequence__organization=organization,
        )
        return {
            "tool": tool,
            "arguments": {
                "cadence_id": str(step.sequence_id),
                "step_id": str(step.id),
                "data": _snapshot_step_for_inverse(step),
            },
        }
    if tool == "reorder_cadence_steps":
        sequence = FollowupSequence.objects.get(
            pk=arguments["cadence_id"], organization=organization
        )
        return {
            "tool": tool,
            "arguments": {
                "cadence_id": str(sequence.id),
                "step_ids": [
                    str(item)
                    for item in sequence.steps.order_by("position", "created_at")
                    .values_list("id", flat=True)
                ],
            },
        }
    if tool == "upsert_touchpoint" and arguments.get("touchpoint_id"):
        obj = TouchpointReply.objects.select_related("category").get(
            pk=arguments["touchpoint_id"],
            category__organization=organization,
        )
        return {
            "tool": "upsert_touchpoint",
            "arguments": {
                "touchpoint_id": str(obj.id),
                "data": {
                    "category_id": str(obj.category_id),
                    "title": obj.title,
                    "body": obj.body,
                },
            },
        }
    if tool == "archive_touchpoint":
        obj = TouchpointReply.objects.select_related("category").get(
            pk=arguments["touchpoint_id"],
            category__organization=organization,
        )
        return {
            "tool": "upsert_touchpoint",
            "arguments": {
                "touchpoint_id": str(obj.id),
                "data": {
                    "category_id": str(obj.category_id),
                    "title": obj.title,
                    "body": obj.body,
                },
            },
        }
    if tool == "upsert_faq" and arguments.get("faq_id"):
        obj = FAQ.objects.get(pk=arguments["faq_id"], organization=organization)
        return {
            "tool": "upsert_faq",
            "arguments": {
                "faq_id": str(obj.id),
                "data": {
                    "question": obj.question,
                    "answer": obj.answer,
                    "is_active": obj.is_active,
                },
            },
        }
    if tool == "archive_faq":
        obj = FAQ.objects.get(pk=arguments["faq_id"], organization=organization)
        return {
            "tool": "upsert_faq",
            "arguments": {
                "faq_id": str(obj.id),
                "data": {
                    "question": obj.question,
                    "answer": obj.answer,
                    "is_active": obj.is_active,
                },
            },
        }
    if tool == "update_messaging_automation_settings":
        account = WhatsAppAccount.objects.filter(
            pk=arguments["whatsapp_account_id"],
            organization=organization,
            is_active=True,
        ).defer("access_token").first()
        if account is None:
            raise OperationsToolError("WhatsApp account not found for plan rollback snapshot.")
        return {
            "tool": tool,
            "arguments": {
                "whatsapp_account_id": str(account.id),
                "changes": get_session_settings(account=account),
            },
        }
    if tool == "reorder_stages":
        pipeline = Pipeline.objects.get(
            pk=arguments["pipeline_id"], organization=organization
        )
        return {
            "tool": tool,
            "arguments": {
                "pipeline_id": str(pipeline.id),
                "stage_ids": [
                    str(item)
                    for item in pipeline.stages.order_by("display_order", "name", "id")
                    .values_list("id", flat=True)
                ],
            },
        }
    return None


def apply_configuration_plan(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_CONFIGURATION_PLAN_WRITE,
    )
    if "operations.write" not in identity.scopes:
        raise OperationsPermissionError(
            "This OAuth token does not include operations.write."
        )
    reason = _reason(arguments, required=True)
    plan_id = _uuid((arguments or {}).get("plan_id"), field="plan_id")

    # Preflight before consuming the one-use human approval receipt. The actual
    # mutation then re-locks/re-checks everything in a single inner transaction.
    preflight = OperationsConfigurationPlan.objects.filter(
        pk=plan_id,
        organization=organization,
        actor=identity.actor,
        token=identity.token,
        role=identity.role,
    ).first()
    if preflight is None:
        raise OperationsPermissionError(
            "Configuration plan not found for this actor, OAuth grant, and organization."
        )
    if preflight.status == OperationsConfigurationPlan.Status.APPLIED:
        return ToolExecution(
            data={
                "status": "ALREADY_APPLIED",
                "plan_id": str(preflight.id),
                "applied_etag": preflight.applied_etag,
                "result": preflight.apply_result,
            },
            capability=CAP_CONFIGURATION_PLAN_WRITE,
            target_type="configuration_plan",
            target_id=str(preflight.id),
            reason=reason,
            audit_summary={"operation": "apply_configuration_plan", "idempotent": True},
        )
    if preflight.status != OperationsConfigurationPlan.Status.READY:
        raise OperationsToolError("Configuration plan is not in a ready state.")
    if preflight.expires_at <= timezone.now():
        OperationsConfigurationPlan.objects.filter(pk=preflight.pk).update(
            status=OperationsConfigurationPlan.Status.EXPIRED
        )
        raise OperationsApprovalRequired(
            "Configuration plan expired. Create and approve a fresh plan."
        )
    if configuration_etag(organization) != preflight.base_etag:
        raise OperationsApprovalRequired(
            "Organization configuration changed after plan review. "
            "Create a fresh plan against the current configuration."
        )

    # This receipt is consumed for the execution attempt even if a later member
    # fails and the configuration transaction is fully rolled back.
    _consume_plan_approval(
        identity=identity,
        organization=organization,
        plan=preflight,
        arguments=arguments,
    )

    with transaction.atomic():
        plan = (
            OperationsConfigurationPlan.objects.select_for_update()
            .filter(
                pk=plan_id,
                organization=organization,
                actor=identity.actor,
                token=identity.token,
                role=identity.role,
            )
            .first()
        )
        if plan is None or plan.status != OperationsConfigurationPlan.Status.READY:
            raise OperationsApprovalRequired(
                "Configuration plan changed after execution began. Create a fresh plan."
            )

        OrganizationModel = organization.__class__
        organization = OrganizationModel.objects.select_for_update().get(pk=organization.pk)
        current_etag = configuration_etag(organization)
        if current_etag != plan.base_etag:
            raise OperationsApprovalRequired(
                "Organization configuration changed after approval. "
                "Create a fresh plan against the current configuration."
            )

        refs = {}
        results = []
        inverse = []
        with configuration_plan_execution():
            for item in plan.operations:
                resolved = _resolve_refs(item["arguments"], refs)
                if plan.reversible:
                    captured = _capture_inverse(
                        organization=organization,
                        tool=item["tool"],
                        arguments=resolved,
                    )
                    if captured is None:
                        raise OperationsToolError(
                            "Plan was marked reversible but an inverse operation could not be captured."
                        )
                    inverse.append(captured)
                member_args = deepcopy(resolved)
                member_args.update(
                    {
                        "dry_run": False,
                        "approved": False,
                        "reason": plan.reason,
                    }
                )
                execution = _execute_plan_member(
                    identity=identity,
                    tool=item["tool"],
                    arguments=member_args,
                )
                safe_data = sanitize_data(execution.data)
                refs[item["ref"]] = {
                    "target_id": execution.target_id,
                    "data": safe_data,
                }
                results.append(
                    {
                        "ref": item["ref"],
                        "tool": item["tool"],
                        "target_type": execution.target_type,
                        "target_id": execution.target_id,
                        "result": safe_data,
                    }
                )

        after_etag = configuration_etag(organization)
        plan.status = OperationsConfigurationPlan.Status.APPLIED
        plan.apply_result = {
            "results": results,
            "operation_count": len(results),
        }
        plan.inverse_operations = inverse
        plan.applied_etag = after_etag
        plan.applied_at = timezone.now()
        plan.save(
            update_fields=[
                "status",
                "apply_result",
                "inverse_operations",
                "applied_etag",
                "applied_at",
                "updated_at",
            ]
        )

    return ToolExecution(
        data={
            "status": "APPLIED",
            "plan_id": str(plan.id),
            "operation_count": len(results),
            "base_configuration_etag": plan.base_etag,
            "applied_configuration_etag": plan.applied_etag,
            "reversible_after_success": plan.reversible,
            "verification": "passed",
            "results": results,
        },
        capability=CAP_CONFIGURATION_PLAN_WRITE,
        target_type="configuration_plan",
        target_id=str(plan.id),
        reason=reason,
        audit_summary={
            "operation": "apply_configuration_plan",
            "plan_digest": plan.plan_hash[:32],
            "operation_count": len(results),
            "applied_etag": plan.applied_etag,
            "verification": "passed",
        },
    )


def rollback_configuration_plan(*, identity, arguments):
    from apps.integrations.operations_tools import _write_gate

    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CONFIGURATION_PLAN_WRITE,
        tool_name="rollback_configuration_plan",
        arguments=arguments,
    )
    plan_id = _uuid((arguments or {}).get("plan_id"), field="plan_id")
    plan = OperationsConfigurationPlan.objects.filter(
        pk=plan_id,
        organization=organization,
        actor=identity.actor,
    ).first()
    if plan is None:
        raise OperationsToolError("Configuration plan not found in this organization.")
    if plan.status != OperationsConfigurationPlan.Status.APPLIED:
        raise OperationsToolError("Only an applied configuration plan can be rolled back.")
    if not plan.reversible or not plan.inverse_operations:
        raise OperationsPermissionError(
            "This plan contains creations or otherwise non-recoverable operations. "
            "Its failed apply would still have rolled back atomically, but a later "
            "post-success rollback is not available."
        )
    if configuration_etag(organization) != plan.applied_etag:
        raise OperationsApprovalRequired(
            "Organization configuration changed after this plan was applied. "
            "Automatic rollback is blocked to avoid overwriting newer changes."
        )

    proposal = {
        "plan_id": str(plan.id),
        "plan_digest": plan.plan_hash[:32],
        "current_etag": plan.applied_etag,
        "target_etag": plan.base_etag,
        "inverse_operation_count": len(plan.inverse_operations),
    }
    from apps.integrations.operations_tools import _ensure_approved_proposal_unchanged, _proposal_digest
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "plan_id": str(plan.id),
                "inverse_operation_count": len(plan.inverse_operations),
                "current_configuration_etag": plan.applied_etag,
                "target_configuration_etag": plan.base_etag,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CONFIGURATION_PLAN_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CONFIGURATION_PLAN_WRITE,
            target_type="configuration_plan",
            target_id=str(plan.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "rollback_configuration_plan",
                "plan_digest": plan.plan_hash[:32],
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    with transaction.atomic():
        plan = OperationsConfigurationPlan.objects.select_for_update().get(
            pk=plan.pk,
            organization=organization,
        )
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        if configuration_etag(organization) != plan.applied_etag:
            raise OperationsApprovalRequired(
                "Organization configuration changed after rollback review."
            )
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        rollback_results = []
        with configuration_plan_execution():
            for inverse in reversed(plan.inverse_operations):
                member_args = deepcopy(inverse["arguments"])
                member_args.update(
                    {
                        "dry_run": False,
                        "approved": False,
                        "reason": f"Rollback configuration plan {plan.id}.",
                    }
                )
                execution = _execute_plan_member(
                    identity=identity,
                    tool=inverse["tool"],
                    arguments=member_args,
                )
                rollback_results.append(
                    {
                        "tool": inverse["tool"],
                        "target_type": execution.target_type,
                        "target_id": execution.target_id,
                    }
                )
        rolled_back_etag = configuration_etag(organization)
        if rolled_back_etag != plan.base_etag:
            raise OperationsToolError(
                "Configuration rollback read-back did not match the plan's base ETag."
            )
        plan.status = OperationsConfigurationPlan.Status.ROLLED_BACK
        plan.rolled_back_at = timezone.now()
        plan.save(update_fields=["status", "rolled_back_at", "updated_at"])

    return ToolExecution(
        data={
            "status": "ROLLED_BACK",
            "plan_id": str(plan.id),
            "configuration_etag": rolled_back_etag,
            "operation_count": len(rollback_results),
            "verification": "passed",
        },
        capability=CAP_CONFIGURATION_PLAN_WRITE,
        target_type="configuration_plan",
        target_id=str(plan.id),
        reason=reason,
        audit_summary={
            "operation": "rollback_configuration_plan",
            "plan_digest": plan.plan_hash[:32],
            "verification": "passed",
        },
    )


def _find_account_by_ref(organization, ref):
    for account in (
        WhatsAppAccount.objects.filter(organization=organization, is_active=True)
        .defer("access_token")
    ):
        if _account_ref(account) == ref:
            return account
    return None


def _import_operations(organization, configuration):
    if not isinstance(configuration, dict):
        raise OperationsToolError("configuration must be an exported SHVYA configuration object.")
    if int(configuration.get("schema_version") or 0) != EXPORT_SCHEMA_VERSION:
        raise OperationsToolError(
            f"Unsupported configuration schema_version; expected {EXPORT_SCHEMA_VERSION}."
        )

    operations = []
    pipeline_refs = {}
    stage_refs = {}
    cadence_refs = {}

    for index, pipeline_data in enumerate(configuration.get("pipelines") or [], start=1):
        name = str(pipeline_data.get("name") or "").strip()
        existing = Pipeline.objects.filter(
            organization=organization,
            name__iexact=name,
        ).first()
        ref = f"pipeline_{index}"
        pipeline_refs[name.casefold()] = (
            str(existing.id) if existing else {"$ref": f"{ref}.target_id"}
        )
        operations.append(
            {
                "ref": ref,
                "tool": "upsert_pipeline_configuration",
                "arguments": {
                    **({"pipeline_id": str(existing.id)} if existing else {}),
                    "data": {
                        "name": name,
                        "description": str(pipeline_data.get("description") or ""),
                        "is_active": bool(pipeline_data.get("active", True)),
                        "ai_enabled": bool(pipeline_data.get("ai_enabled", True)),
                    },
                },
            }
        )
        ordered_stage_refs = []
        for stage_index, stage_data in enumerate(pipeline_data.get("stages") or [], start=1):
            stage_name = str(stage_data.get("name") or "").strip()
            existing_stage = (
                Stage.objects.filter(
                    pipeline=existing,
                    name__iexact=stage_name,
                ).first()
                if existing else None
            )
            stage_ref = f"stage_{index}_{stage_index}"
            stage_value = (
                str(existing_stage.id)
                if existing_stage
                else {"$ref": f"{stage_ref}.target_id"}
            )
            stage_refs[(name.casefold(), stage_name.casefold())] = stage_value
            ordered_stage_refs.append(stage_value)
            pipeline_value = (
                str(existing.id)
                if existing
                else {"$ref": f"{ref}.target_id"}
            )
            operations.append(
                {
                    "ref": stage_ref,
                    "tool": "_ensure_stage_configuration",
                    "arguments": {
                        "pipeline_id": pipeline_value,
                        **({"stage_id": str(existing_stage.id)} if existing_stage else {}),
                        "data": {
                            "name": stage_name,
                            "description": str(stage_data.get("description") or ""),
                            "is_active": bool(stage_data.get("active", True)),
                            "ai_on": bool(stage_data.get("ai_on", True)),
                        },
                    },
                }
            )
        if ordered_stage_refs:
            operations.append(
                {
                    "ref": f"pipeline_{index}_stage_order",
                    "tool": "reorder_stages",
                    "arguments": {
                        "pipeline_id": (
                            str(existing.id)
                            if existing
                            else {"$ref": f"{ref}.target_id"}
                        ),
                        "stage_ids": ordered_stage_refs,
                    },
                }
            )

    for index, attribute in enumerate(configuration.get("attributes") or [], start=1):
        key = str(attribute.get("key") or "").strip()
        existing = AttributeDefinition.objects.filter(
            organization=organization,
            key=key,
        ).first()
        operations.append(
            {
                "ref": f"attribute_{index}",
                "tool": "upsert_attribute_configuration",
                "arguments": {
                    **({"attribute_id": str(existing.id)} if existing else {}),
                    "data": {
                        "name": str(attribute.get("name") or ""),
                        "field_type": str(attribute.get("field_type") or "text"),
                        "description": str(attribute.get("description") or ""),
                        "options": list(attribute.get("options") or []),
                    },
                },
            }
        )

    ai = configuration.get("ai") or {}
    if ai:
        operations.append(
            {
                "ref": "ai_configuration",
                "tool": "update_ai_configuration",
                "arguments": {
                    "changes": {
                        key: ai[key]
                        for key in (
                            "about",
                            "bot_languages",
                            "ai_playbook",
                            "ai_enabled",
                            "bump_up_enabled",
                            "bump_up_count",
                        )
                        if key in ai
                    }
                },
            }
        )

    account_refs = {}
    missing_accounts = []
    for account_data in configuration.get("whatsapp_accounts") or []:
        ref = str(account_data.get("account_ref") or "")
        target = _find_account_by_ref(organization, ref)
        if target:
            account_refs[ref] = str(target.id)
        else:
            missing_accounts.append(ref)
    if missing_accounts:
        raise OperationsPermissionError(
            "Import requires matching connected WhatsApp accounts in the target "
            "organization before Cadence/Workflow/messaging configuration can be "
            "migrated. Missing account refs: " + ", ".join(sorted(set(missing_accounts)))
        )

    for index, cadence in enumerate(configuration.get("cadences") or [], start=1):
        name = str(cadence.get("name") or "").strip()
        existing = FollowupSequence.objects.filter(
            organization=organization,
            name__iexact=name,
        ).first()
        ref = f"cadence_{index}"
        cadence_refs[name.casefold()] = (
            str(existing.id) if existing else {"$ref": f"{ref}.target_id"}
        )
        account_ref = str(cadence.get("account_ref") or "")
        operations.append(
            {
                "ref": ref,
                "tool": "upsert_cadence_configuration",
                "arguments": {
                    **({"cadence_id": str(existing.id)} if existing else {}),
                    "data": {
                        "name": name,
                        "description": str(cadence.get("description") or ""),
                        "provider": str(cadence.get("provider") or "api"),
                        "whatsapp_account_id": account_refs[account_ref],
                    },
                },
            }
        )
        if not existing:
            cadence_target = {"$ref": f"{ref}.target_id"}
            for step_index, step in enumerate(cadence.get("steps") or [], start=1):
                if not step.get("active", True):
                    continue
                schedule = deepcopy(step.get("schedule") or {})
                if cadence.get("provider") == "hosted" and step.get("type") == "whatsapp":
                    if step.get("hosted_attachment"):
                        raise OperationsPermissionError(
                            "Portable import cannot reconstruct Hosted attachment binaries. "
                            f"Cadence '{name}' step {step_index} must be reattached manually."
                        )
                    operations.append(
                        {
                            "ref": f"cadence_{index}_step_{step_index}",
                            "tool": "add_hosted_whatsapp_step",
                            "arguments": {
                                "cadence_id": cadence_target,
                                "data": {
                                    "title": str(step.get("title") or ""),
                                    "body": str(step.get("hosted_body") or ""),
                                    "schedule": schedule,
                                },
                            },
                        }
                    )
                elif step.get("type") == "email":
                    operations.append(
                        {
                            "ref": f"cadence_{index}_step_{step_index}",
                            "tool": "add_cadence_step",
                            "arguments": {
                                "cadence_id": cadence_target,
                                "data": {
                                    "type": "email",
                                    "title": str(step.get("title") or ""),
                                    "subject": str(step.get("email_subject") or ""),
                                    "body": str(step.get("email_body") or ""),
                                    "schedule": schedule,
                                },
                            },
                        }
                    )
                elif step.get("type") == "reminder":
                    operations.append(
                        {
                            "ref": f"cadence_{index}_step_{step_index}",
                            "tool": "add_cadence_step",
                            "arguments": {
                                "cadence_id": cadence_target,
                                "data": {
                                    "type": "reminder",
                                    "text": str(step.get("reminder_text") or ""),
                                    "schedule": schedule,
                                },
                            },
                        }
                    )
                elif step.get("type") == "whatsapp":
                    template = (
                        WhatsAppTemplate.objects.filter(
                            organization=organization,
                            account_id=account_refs[account_ref],
                            name__iexact=str(step.get("template_name") or ""),
                            status=WhatsAppTemplate.Status.APPROVED,
                        ).first()
                    )
                    if template is None:
                        raise OperationsPermissionError(
                            "Portable import requires an approved target-org WhatsApp "
                            f"template named '{step.get('template_name') or ''}' for "
                            f"Cadence '{name}'."
                        )
                    operations.append(
                        {
                            "ref": f"cadence_{index}_step_{step_index}",
                            "tool": "add_cadence_step",
                            "arguments": {
                                "cadence_id": cadence_target,
                                "data": {
                                    "type": "whatsapp",
                                    "template_id": str(template.id),
                                    "retry_count": int(step.get("retry_count") or 0),
                                    "schedule": schedule,
                                },
                            },
                        }
                    )

    for index, workflow in enumerate(configuration.get("workflows") or [], start=1):
        existing = SmartTrigger.objects.filter(
            organization=organization,
            name__iexact=str(workflow.get("name") or ""),
        ).first()
        conditions = deepcopy(workflow.get("conditions") or {})
        conditions["scopes"] = [
            {
                "pipeline": pipeline_refs[str(scope.get("pipeline_name") or "").casefold()],
                "stages": [
                    stage_refs[
                        (
                            str(scope.get("pipeline_name") or "").casefold(),
                            str(stage_name or "").casefold(),
                        )
                    ]
                    for stage_name in scope.get("stage_names") or []
                ],
            }
            for scope in conditions.get("scopes") or []
        ]
        if "sequence_names" in conditions:
            conditions["sequences"] = [
                cadence_refs[str(name or "").casefold()]
                for name in conditions.pop("sequence_names")
            ]

        action = deepcopy(workflow.get("action") or {})
        action_type = str(workflow.get("action_type") or "")
        if action_type == "move_stage":
            pipeline_name = str(action.pop("pipeline_name", "") or "")
            stage_name = str(action.pop("stage_name", "") or "")
            action.update(
                {
                    "pipeline": pipeline_refs[pipeline_name.casefold()],
                    "stage": stage_refs[(pipeline_name.casefold(), stage_name.casefold())],
                }
            )
        elif action_type == "start_sequence":
            sequence_name = str(action.pop("sequence_name", "") or "")
            action["sequence"] = cadence_refs[sequence_name.casefold()]
        elif action_type == "message":
            account_ref = str(action.pop("account_ref", "") or "")
            action["account"] = account_refs[account_ref]

        operations.append(
            {
                "ref": f"workflow_{index}",
                "tool": "upsert_workflow_configuration",
                "arguments": {
                    **({"workflow_id": str(existing.id)} if existing else {}),
                    "data": {
                        "name": str(workflow.get("name") or ""),
                        "enabled": bool(workflow.get("enabled", False)),
                        "trigger_type": str(workflow.get("trigger_type") or ""),
                        "conditions": conditions,
                        "action_type": action_type,
                        "action": action,
                    },
                },
            }
        )

    for category_index, category in enumerate(configuration.get("touchpoints") or [], start=1):
        category_name = str(category.get("name") or "")
        target_category = TouchpointCategory.objects.filter(
            organization=organization,
            name__iexact=category_name,
        ).first()
        for reply_index, reply in enumerate(category.get("replies") or [], start=1):
            existing = (
                TouchpointReply.objects.filter(
                    category=target_category,
                    title__iexact=str(reply.get("title") or ""),
                ).first()
                if target_category else None
            )
            operations.append(
                {
                    "ref": f"touchpoint_{category_index}_{reply_index}",
                    "tool": "upsert_touchpoint",
                    "arguments": {
                        **({"touchpoint_id": str(existing.id)} if existing else {}),
                        "data": {
                            **(
                                {"category_id": str(target_category.id)}
                                if target_category
                                else {"category_name": category_name}
                            ),
                            "title": str(reply.get("title") or ""),
                            "body": str(reply.get("body") or ""),
                        },
                    },
                }
            )

    for index, faq in enumerate(configuration.get("faqs") or [], start=1):
        existing = FAQ.objects.filter(
            organization=organization,
            question__iexact=str(faq.get("question") or ""),
        ).first()
        operations.append(
            {
                "ref": f"faq_{index}",
                "tool": "upsert_faq",
                "arguments": {
                    **({"faq_id": str(existing.id)} if existing else {}),
                    "data": {
                        "question": str(faq.get("question") or ""),
                        "answer": str(faq.get("answer") or ""),
                        "is_active": bool(faq.get("active", True)),
                    },
                },
            }
        )

    for index, messaging in enumerate(configuration.get("messaging") or [], start=1):
        account_ref = str(messaging.get("account_ref") or "")
        operations.append(
            {
                "ref": f"messaging_{index}",
                "tool": "update_messaging_automation_settings",
                "arguments": {
                    "whatsapp_account_id": account_refs[account_ref],
                    "changes": deepcopy(messaging.get("settings") or {}),
                },
            }
        )

    return operations


def import_organization_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_CONFIGURATION_PLAN_WRITE,
    )
    if "operations.write" not in identity.scopes:
        raise OperationsPermissionError(
            "This OAuth token does not include operations.write."
        )
    configuration = (arguments or {}).get("configuration")
    operations = _import_operations(organization, configuration)
    if len(operations) > PLAN_MAX_OPERATIONS:
        raise OperationsToolError(
            "Imported configuration expands beyond the maximum safe plan size."
        )
    plan_arguments = {
        "operations": operations,
        "reason": _reason(arguments, required=True),
        "idempotency_key": str((arguments or {}).get("idempotency_key") or "")[:128],
        "ttl_minutes": (arguments or {}).get("ttl_minutes") or PLAN_TTL_MINUTES,
        "_allow_internal": True,
    }
    execution = create_configuration_plan(
        identity=identity,
        arguments=plan_arguments,
    )
    execution.data["import"] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "source_organization": deepcopy((configuration or {}).get("organization") or {}),
        "target_organization": {
            "id": str(organization.id),
            "name": organization.name,
        },
        "note": (
            "Import creates an approval plan only. It does not mutate configuration "
            "until apply_configuration_plan consumes the plan approval receipt."
        ),
    }
    execution.audit_summary = {
        **(execution.audit_summary or {}),
        "operation": "import_organization_configuration",
    }
    return execution


CONFIGURATION_MANAGEMENT_HANDLERS = {
    "get_configuration_dependency_graph": get_configuration_dependency_graph,
    "validate_organization_configuration": validate_organization_configuration,
    "reorder_stages": reorder_stages,
    "create_configuration_plan": create_configuration_plan,
    "apply_configuration_plan": apply_configuration_plan,
    "rollback_configuration_plan": rollback_configuration_plan,
    "export_organization_configuration": export_organization_configuration,
    "import_organization_configuration": import_organization_configuration,
}
