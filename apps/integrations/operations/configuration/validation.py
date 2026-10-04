# ruff: noqa: F401
"""Dependency graph, validation, and stage ordering for Operations MCP configuration."""

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

from apps.integrations.operations.configuration.export import (
    _object_etag,
    configuration_etag,
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
        .exclude(key="booked_at")
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

    from apps.integrations.operations.tools.qualification import _qualification_public_snapshot

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

    from apps.integrations.operations.tools.qualification import _qualification_public_snapshot
    from apps.integrations.operations.tools.whatsapp import _routing_snapshot

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
            errors.append(
                {
                    "code": "invalid_workflow",
                    "workflow_id": str(rule.id),
                    "workflow_name": rule.name,
                    "detail": list(exc.messages),
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
            sequence.whatsapp_account_id
            and sequence.whatsapp_account.connection_type
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
        "counts": {
            "errors": len(errors),
            "warnings": len(warnings),
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
