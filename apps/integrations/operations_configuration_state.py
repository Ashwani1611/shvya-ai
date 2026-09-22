"""Versioned, tenant-safe configuration state for SHVYA Operations MCP."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy

from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from apps.ai_engagement.models import Document, FAQ, KnowledgeSource, OrgInfo
from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.followups.models import (
    FollowupExecution,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
    TouchpointCategory,
    TouchpointReply,
)
from apps.hosted_automation.models import HostedFollowupStepConfig
from apps.integrations.diagnostic_auth import sanitize_text
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_ORGANIZATION_READ,
    CAP_STAGE_CONFIG_WRITE,
    approval_required,
)
from apps.triggers.models import SmartTrigger, TriggerRun
from services.channels.hosted_whatsapp_service import (
    get_pipeline_for_account,
    get_session_settings,
    pipeline_whatsapp_number,
)
from services.triggers.rules import validate as validate_workflow_rule

from apps.integrations.operations_tools import (
    OperationsApprovalRequired,
    OperationsPermissionError,
    OperationsToolError,
    ToolExecution,
    _assert_workflow_safe_attribute_references,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _require_operations_capability,
    _safe_workflow_config,
    _sensitive_attribute_keys,
    _uuid,
    _workflow_reference_index,
    _assert_workflow_tenant_references,
    _write_gate,
)


CONFIGURATION_FORMAT_VERSION = 1


def _raw_configuration_state(organization):
    """Return a deterministic internal state used only for semantic ETags."""

    info = OrgInfo.objects.filter(organization=organization).first()
    pipelines = list(
        Pipeline.objects.filter(organization=organization)
        .order_by("id")
        .values(
            "id",
            "name",
            "description",
            "country_code",
            "phone_number",
            "owner_id",
            "is_active",
            "ai_enabled",
        )
    )
    stages = list(
        Stage.objects.filter(pipeline__organization=organization)
        .order_by("id")
        .values(
            "id",
            "pipeline_id",
            "name",
            "description",
            "display_order",
            "color",
            "is_active",
            "ai_on",
            "config",
        )
    )
    attributes = list(
        AttributeDefinition.objects.filter(organization=organization)
        .order_by("id")
        .values(
            "id",
            "name",
            "key",
            "field_type",
            "description",
            "options",
            "display_order",
        )
    )
    workflows = list(
        SmartTrigger.objects.filter(organization=organization)
        .order_by("id")
        .values(
            "id",
            "name",
            "enabled",
            "position",
            "trigger_type",
            "conditions",
            "action_type",
            "action",
            "fingerprint",
        )
    )
    cadences = list(
        FollowupSequence.objects.filter(organization=organization)
        .order_by("id")
        .values(
            "id",
            "name",
            "description",
            "whatsapp_account_id",
            "is_active",
        )
    )
    steps = list(
        FollowupStep.objects.filter(sequence__organization=organization)
        .order_by("id")
        .values(
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
            "retry_delay_hours",
            "is_active",
        )
    )
    hosted_configs = list(
        HostedFollowupStepConfig.objects.filter(
            step__sequence__organization=organization
        )
        .order_by("id")
        .values(
            "id",
            "step_id",
            "body",
            "attachment_original_name",
            "attachment_mime_type",
            "attachment_size",
            "authored_content_hash",
        )
    )
    faqs = list(
        FAQ.objects.filter(organization=organization)
        .order_by("id")
        .values("id", "question", "answer", "is_active")
    )
    touchpoint_categories = list(
        TouchpointCategory.objects.filter(organization=organization)
        .order_by("id")
        .values("id", "name")
    )
    touchpoints = list(
        TouchpointReply.objects.filter(category__organization=organization)
        .order_by("id")
        .values("id", "category_id", "title", "body", "is_active")
    )
    accounts = list(
        WhatsAppAccount.objects.filter(organization=organization)
        .defer("access_token")
        .order_by("id")
    )
    account_state = []
    for account in accounts:
        pipeline = get_pipeline_for_account(account=account)
        account_state.append(
            {
                "id": str(account.id),
                "connection_type": account.connection_type,
                "business_name": account.business_name,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
                "is_active": account.is_active,
                "pipeline_id": str(pipeline.id) if pipeline else None,
                "settings": get_session_settings(account=account),
            }
        )
    knowledge_sources = list(
        KnowledgeSource.objects.filter(organization=organization)
        .order_by("id")
        .values("id", "source_type", "name", "url", "is_active")
    )
    documents = list(
        Document.objects.filter(organization=organization)
        .order_by("id")
        .values(
            "id",
            "name",
            "source_key",
            "version",
            "source_url",
            "share_instruction",
            "processing_status",
            "is_active",
        )
    )

    return {
        "organization": {
            "id": str(organization.id),
            "name": organization.name,
            "timezone": organization.timezone,
        },
        "ai": {
            "about": str(getattr(info, "about", "") or ""),
            "bot_languages": str(getattr(info, "bot_languages", "") or ""),
            "ai_playbook": str(getattr(info, "ai_playbook", "") or ""),
            "ai_enabled": bool(getattr(info, "ai_enabled", True)) if info else None,
            "bump_up_enabled": bool(getattr(info, "bump_up_enabled", True)) if info else None,
            "bump_up_count": int(getattr(info, "bump_up_count", 0)) if info else None,
        },
        "pipelines": pipelines,
        "stages": stages,
        "attributes": attributes,
        "workflows": workflows,
        "cadences": cadences,
        "cadence_steps": steps,
        "hosted_step_configs": hosted_configs,
        "faqs": faqs,
        "touchpoint_categories": touchpoint_categories,
        "touchpoints": touchpoints,
        "messaging_accounts": account_state,
        "knowledge_sources": knowledge_sources,
        "knowledge_documents": documents,
    }


def configuration_etag(organization):
    raw = json.dumps(
        _raw_configuration_state(organization),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _workflow_portable(rule, *, pipeline_names, stage_rows, cadence_names, account_numbers):
    conditions = deepcopy(rule.conditions if isinstance(rule.conditions, dict) else {})
    action = deepcopy(rule.action if isinstance(rule.action, dict) else {})

    scopes = []
    for scope in conditions.get("scopes") or []:
        if not isinstance(scope, dict):
            continue
        pipeline_id = str(scope.get("pipeline") or "")
        scopes.append(
            {
                "pipeline": pipeline_names.get(pipeline_id, "[MISSING_PIPELINE]"),
                "stages": [
                    stage_rows.get((pipeline_id, str(stage_id)), "[MISSING_STAGE]")
                    for stage_id in scope.get("stages") or []
                ],
            }
        )
    if "scopes" in conditions:
        conditions["scopes"] = scopes
    if "sequences" in conditions:
        conditions["sequences"] = [
            cadence_names.get(str(item), "[MISSING_CADENCE]")
            for item in conditions.get("sequences") or []
        ]

    if rule.action_type == "move_stage":
        pipeline_id = str(action.get("pipeline") or "")
        stage_id = str(action.get("stage") or "")
        action = {
            "pipeline": pipeline_names.get(pipeline_id, "[MISSING_PIPELINE]"),
            "stage": stage_rows.get((pipeline_id, stage_id), "[MISSING_STAGE]"),
        }
    elif rule.action_type == "start_sequence":
        action["sequence"] = cadence_names.get(
            str(action.get("sequence") or ""),
            "[MISSING_CADENCE]",
        )
    elif rule.action_type == "message":
        action["account"] = account_numbers.get(
            str(action.get("account") or ""),
            "[MISSING_WHATSAPP_ACCOUNT]",
        )

    return {
        "name": rule.name,
        "enabled": rule.enabled,
        "position": rule.position,
        "trigger_type": rule.trigger_type,
        "conditions": conditions,
        "action_type": rule.action_type,
        "action": action,
    }


def portable_configuration(organization):
    """Export a portable, secret-free configuration document."""

    from apps.integrations.operations_extended_tools import (
        _qualification_public_snapshot,
        _step_snapshot,
    )

    sensitive_keys = _sensitive_attribute_keys(organization)
    pipelines = list(
        Pipeline.objects.filter(organization=organization)
        .order_by("name", "id")
    )
    pipeline_names = {str(item.id): item.name for item in pipelines}
    stage_rows = {}
    portable_pipelines = []
    for pipeline in pipelines:
        stages = list(
            pipeline.stages.all().order_by("display_order", "name", "id")
        )
        for stage in stages:
            stage_rows[(str(pipeline.id), str(stage.id))] = stage.name
        portable_pipelines.append(
            {
                "name": pipeline.name,
                "description": pipeline.description,
                "country_code": pipeline.country_code,
                "phone_number": pipeline.phone_number,
                "is_active": pipeline.is_active,
                "ai_enabled": pipeline.ai_enabled,
                "stages": [
                    {
                        "name": stage.name,
                        "description": stage.description,
                        "display_order": stage.display_order,
                        "color": stage.color,
                        "is_active": stage.is_active,
                        "ai_on": stage.ai_on,
                        "config": deepcopy(stage.config or {}),
                    }
                    for stage in stages
                ],
            }
        )

    attributes = [
        {
            "name": item.name,
            "key": item.key,
            "field_type": item.field_type,
            "description": item.description,
            "options": list(item.options or []),
            "display_order": item.display_order,
        }
        for item in AttributeDefinition.objects.filter(
            organization=organization
        ).exclude(key__in=sensitive_keys).order_by("display_order", "name")
    ]

    accounts = list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            is_active=True,
        ).defer("access_token").order_by("business_name", "id")
    )
    account_numbers = {
        str(item.id): item.display_phone_number
        for item in accounts
    }
    cadences = list(
        FollowupSequence.objects.filter(organization=organization)
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
        .order_by("name", "id")
    )
    cadence_names = {str(item.id): item.name for item in cadences}

    workflows = []
    for rule in SmartTrigger.objects.filter(organization=organization).order_by(
        "position", "name", "id"
    ):
        conditions, action, redacted = _safe_workflow_config(rule, sensitive_keys)
        safe_rule = deepcopy(rule)
        safe_rule.conditions = conditions
        safe_rule.action = action
        row = _workflow_portable(
            safe_rule,
            pipeline_names=pipeline_names,
            stage_rows=stage_rows,
            cadence_names=cadence_names,
            account_numbers=account_numbers,
        )
        row["sensitive_fields_redacted"] = bool(redacted)
        workflows.append(row)

    portable_cadences = []
    for sequence in cadences:
        steps = []
        for step in sequence.steps.order_by("position", "created_at"):
            snapshot = _step_snapshot(
                FollowupStep.objects.select_related(
                    "sequence__whatsapp_account",
                    "whatsapp_template",
                ).get(pk=step.pk)
            )
            row = {
                "position": step.position,
                "type": step.step_type,
                "title": step.title,
                "schedule": deepcopy(snapshot["schedule"]),
                "is_active": step.is_active,
            }
            if step.step_type == FollowupStep.StepType.WHATSAPP:
                if sequence.whatsapp_account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
                    row.update(
                        {
                            "body": snapshot.get("hosted_body") or "",
                            "attachment_name": snapshot.get("hosted_attachment_name") or "",
                            "attachment_requires_reupload": bool(
                                snapshot.get("hosted_attachment_name")
                            ),
                        }
                    )
                else:
                    row["template_name"] = (
                        step.whatsapp_template.name
                        if step.whatsapp_template_id
                        else ""
                    )
                    row["retry_count"] = step.retry_count
            elif step.step_type == FollowupStep.StepType.EMAIL:
                row.update(
                    {
                        "subject": step.email_subject,
                        "body": step.email_body,
                    }
                )
            else:
                row["text"] = step.reminder_text
            steps.append(row)
        portable_cadences.append(
            {
                "name": sequence.name,
                "description": sequence.description,
                "is_active": sequence.is_active,
                "provider": (
                    "api"
                    if sequence.whatsapp_account.connection_type
                    == WhatsAppAccount.ConnectionType.API
                    else "hosted"
                ),
                "whatsapp_display_phone_number": (
                    sequence.whatsapp_account.display_phone_number
                ),
                "steps": steps,
            }
        )

    info = OrgInfo.objects.filter(organization=organization).first()
    ai = {
        "about": str(getattr(info, "about", "") or ""),
        "bot_languages": str(getattr(info, "bot_languages", "") or ""),
        "ai_enabled": bool(getattr(info, "ai_enabled", True)) if info else True,
        "bump_up_enabled": bool(getattr(info, "bump_up_enabled", True)) if info else True,
        "bump_up_count": int(getattr(info, "bump_up_count", 0)) if info else 0,
        "qualification": _qualification_public_snapshot(organization),
    }

    messaging = []
    for account in accounts:
        pipeline = get_pipeline_for_account(account=account)
        messaging.append(
            {
                "connection_type": account.connection_type,
                "business_name": account.business_name,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
                "pipeline": pipeline.name if pipeline else None,
                "settings": deepcopy(get_session_settings(account=account)),
            }
        )

    knowledge_sources = []
    for source in KnowledgeSource.objects.filter(
        organization=organization
    ).order_by("source_type", "name", "id"):
        knowledge_sources.append(
            {
                "source_type": source.source_type,
                "name": source.name,
                "url": source.url if source.source_type == KnowledgeSource.SourceType.URL else "",
                "is_active": source.is_active,
                "requires_file_reupload": source.source_type == KnowledgeSource.SourceType.FILE,
            }
        )

    return {
        "format_version": CONFIGURATION_FORMAT_VERSION,
        "organization": {
            "name": organization.name,
            "timezone": organization.timezone,
        },
        "etag": configuration_etag(organization),
        "ai": ai,
        "pipelines": portable_pipelines,
        "attributes": attributes,
        "workflows": workflows,
        "cadences": portable_cadences,
        "messaging": messaging,
        "faqs": [
            {
                "question": item.question,
                "answer": item.answer,
                "is_active": item.is_active,
            }
            for item in FAQ.objects.filter(organization=organization).order_by(
                "question", "id"
            )
        ],
        "touchpoints": [
            {
                "category": reply.category.name,
                "title": reply.title,
                "body": reply.body,
                "is_active": reply.is_active,
            }
            for reply in TouchpointReply.objects.filter(
                category__organization=organization
            ).select_related("category").order_by(
                "category__name", "title", "id"
            )
        ],
        "knowledge_sources": knowledge_sources,
    }


def export_organization_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    export = portable_configuration(organization)
    redacted_workflows = sum(
        1 for item in export["workflows"] if item.get("sensitive_fields_redacted")
    )
    return ToolExecution(
        data={
            "configuration": export,
            "portable": redacted_workflows == 0,
            "limitations": [
                "Provider credentials, OAuth/session secrets, raw tokens, and environment values are never exported.",
                "Knowledge file bytes are not exported; file sources require re-upload.",
                "WhatsApp accounts are referenced by safe display number only and must already be provisioned in a target organization.",
            ],
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "format_version": CONFIGURATION_FORMAT_VERSION,
            "pipeline_count": len(export["pipelines"]),
            "workflow_count": len(export["workflows"]),
            "cadence_count": len(export["cadences"]),
            "sensitive_workflows_redacted": redacted_workflows,
        },
    )


def _workflow_dependency_rows(organization):
    rows = []
    for rule in SmartTrigger.objects.filter(organization=organization).order_by("id"):
        conditions = rule.conditions if isinstance(rule.conditions, dict) else {}
        action = rule.action if isinstance(rule.action, dict) else {}
        refs = []
        for scope in conditions.get("scopes") or []:
            if not isinstance(scope, dict):
                continue
            pipeline_id = str(scope.get("pipeline") or "")
            if pipeline_id:
                refs.append(("pipeline", pipeline_id, "trigger_scope"))
            for stage_id in scope.get("stages") or []:
                refs.append(("stage", str(stage_id), "trigger_scope"))
        for sequence_id in conditions.get("sequences") or []:
            refs.append(("cadence", str(sequence_id), "trigger_condition"))
        for condition in conditions.get("attributes") or []:
            if isinstance(condition, dict) and condition.get("key"):
                refs.append(("attribute", str(condition["key"]), "trigger_condition"))
        if rule.action_type == "move_stage":
            refs.extend(
                [
                    ("pipeline", str(action.get("pipeline") or ""), "action"),
                    ("stage", str(action.get("stage") or ""), "action"),
                ]
            )
        elif rule.action_type == "start_sequence":
            refs.append(("cadence", str(action.get("sequence") or ""), "action"))
        elif rule.action_type == "message":
            refs.append(("whatsapp_account", str(action.get("account") or ""), "action"))
        elif rule.action_type == "attribute":
            refs.append(("attribute", str(action.get("key") or ""), "action"))
        elif rule.action_type == "reminder" and action.get("date_attribute"):
            refs.append(("attribute", str(action["date_attribute"]), "action"))
        rows.append((rule, [item for item in refs if item[1]]))
    return rows


def get_configuration_dependency_graph(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    resource_type = str((arguments or {}).get("resource_type") or "").strip()
    resource_id = str((arguments or {}).get("resource_id") or "").strip()
    if bool(resource_type) != bool(resource_id):
        raise OperationsToolError(
            "resource_type and resource_id must be supplied together."
        )

    graph = defaultdict(lambda: {"dependencies": [], "counts": {}})
    workflow_rows = _workflow_dependency_rows(organization)
    for rule, refs in workflow_rows:
        for ref_type, ref_id, relation in refs:
            key = f"{ref_type}:{ref_id}"
            graph[key]["dependencies"].append(
                {
                    "type": "workflow",
                    "id": str(rule.id),
                    "name": rule.name,
                    "relation": relation,
                }
            )

    for pipeline in Pipeline.objects.filter(organization=organization):
        key = f"pipeline:{pipeline.id}"
        graph[key]["counts"]["leads"] = Lead.objects.filter(
            organization=organization,
            pipeline=pipeline,
        ).count()
        graph[key]["counts"]["stages"] = pipeline.stages.count()
        account_count = sum(
            1
            for account in WhatsAppAccount.objects.filter(
                organization=organization,
                is_active=True,
            ).defer("access_token")
            if get_pipeline_for_account(account=account) == pipeline
        )
        graph[key]["counts"]["whatsapp_accounts"] = account_count

    for stage in Stage.objects.filter(pipeline__organization=organization):
        key = f"stage:{stage.id}"
        graph[key]["counts"]["leads"] = Lead.objects.filter(
            organization=organization,
            stage=stage,
        ).count()

    for attribute in AttributeDefinition.objects.filter(organization=organization):
        key = f"attribute:{attribute.key}"
        graph[key]["counts"]["leads_with_value"] = Lead.objects.filter(
            organization=organization,
            **{f"attributes__has_key": attribute.key},
        ).count()

    for sequence in FollowupSequence.objects.filter(organization=organization):
        key = f"cadence:{sequence.id}"
        graph[key]["counts"]["steps"] = sequence.steps.count()
        graph[key]["counts"]["lead_states"] = LeadSequenceState.objects.filter(
            organization=organization,
            sequence=sequence,
        ).count()
        graph[key]["counts"]["executions"] = FollowupExecution.objects.filter(
            organization=organization,
            sequence=sequence,
        ).count()

    for account in WhatsAppAccount.objects.filter(organization=organization).defer(
        "access_token"
    ):
        key = f"whatsapp_account:{account.id}"
        graph[key]["counts"]["cadences"] = FollowupSequence.objects.filter(
            organization=organization,
            whatsapp_account=account,
        ).count()
        pipeline = get_pipeline_for_account(account=account)
        if pipeline:
            graph[key]["dependencies"].append(
                {
                    "type": "pipeline",
                    "id": str(pipeline.id),
                    "name": pipeline.name,
                    "relation": "sender_routing",
                }
            )

    from apps.integrations.operations_extended_tools import _qualification_public_snapshot
    qualification = _qualification_public_snapshot(organization)
    completion = qualification.get("completion_stage")
    if completion:
        graph[f"stage:{completion['id']}"]["dependencies"].append(
            {
                "type": "qualification",
                "id": qualification.get("flow_version") or "current",
                "name": "Qualification completion target",
                "relation": "completion_stage",
            }
        )
    for requirement_id, keys in (qualification.get("mappings") or {}).items():
        for key in keys:
            graph[f"attribute:{key}"]["dependencies"].append(
                {
                    "type": "qualification",
                    "id": str(requirement_id),
                    "name": str(requirement_id),
                    "relation": "attribute_mapping",
                }
            )

    if resource_type:
        allowed = {
            "pipeline",
            "stage",
            "attribute",
            "cadence",
            "whatsapp_account",
        }
        if resource_type not in allowed:
            raise OperationsToolError(
                "resource_type must be pipeline, stage, attribute, cadence, or whatsapp_account."
            )
        key = f"{resource_type}:{resource_id}"
        node = graph.get(key, {"dependencies": [], "counts": {}})
        return ToolExecution(
            data={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "dependencies": node["dependencies"][:200],
                "affected_record_counts": node["counts"],
                "dependency_count": len(node["dependencies"]),
                "dependencies_truncated": len(node["dependencies"]) > 200,
            },
            capability=CAP_ORGANIZATION_READ,
            target_type=resource_type,
            target_id=resource_id,
            audit_summary={
                "resource_type": resource_type,
                "dependency_count": len(node["dependencies"]),
            },
        )

    rows = []
    for key in sorted(graph):
        kind, identifier = key.split(":", 1)
        rows.append(
            {
                "resource_type": kind,
                "resource_id": identifier,
                "dependency_count": len(graph[key]["dependencies"]),
                "affected_record_counts": graph[key]["counts"],
            }
        )
    return ToolExecution(
        data={
            "etag": configuration_etag(organization),
            "resources": rows[:500],
            "resource_count": len(rows),
            "resources_truncated": len(rows) > 500,
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"resource_count": len(rows)},
    )


def _detect_stage_workflow_cycles(organization):
    edges = defaultdict(set)
    labels = {}
    for rule in SmartTrigger.objects.filter(
        organization=organization,
        enabled=True,
        trigger_type="stage_moved",
        action_type="move_stage",
    ):
        conditions = rule.conditions if isinstance(rule.conditions, dict) else {}
        action = rule.action if isinstance(rule.action, dict) else {}
        target = str(action.get("stage") or "")
        if not target:
            continue
        labels[str(rule.id)] = rule.name
        for scope in conditions.get("scopes") or []:
            if not isinstance(scope, dict):
                continue
            for source in scope.get("stages") or []:
                source = str(source)
                if source:
                    edges[source].add((target, str(rule.id)))

    cycles = []
    visiting = set()
    visited = set()
    stack = []

    def visit(node):
        if node in visiting:
            if node in stack:
                idx = stack.index(node)
                cycles.append(stack[idx:] + [node])
            return
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for target, _rule_id in edges.get(node, set()):
            visit(target)
        stack.pop()
        visiting.discard(node)
        visited.add(node)

    for node in list(edges):
        visit(node)
    unique = []
    seen = set()
    for cycle in cycles:
        marker = tuple(cycle)
        if marker not in seen:
            unique.append(cycle)
            seen.add(marker)
    return unique[:50]


def validate_organization_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )

    errors = []
    warnings = []

    pipeline_names = Counter(
        item.casefold()
        for item in Pipeline.objects.filter(organization=organization)
        .values_list("name", flat=True)
    )
    for name, count in pipeline_names.items():
        if count > 1:
            errors.append(
                {"code": "duplicate_pipeline_name", "detail": name, "count": count}
            )

    for pipeline in Pipeline.objects.filter(organization=organization):
        stages = list(pipeline.stages.all())
        active_names = Counter(
            item.name.casefold() for item in stages if item.is_active
        )
        orders = Counter(item.display_order for item in stages)
        for name, count in active_names.items():
            if count > 1:
                errors.append(
                    {
                        "code": "duplicate_active_stage_name",
                        "pipeline_id": str(pipeline.id),
                        "detail": name,
                        "count": count,
                    }
                )
        for order, count in orders.items():
            if count > 1:
                errors.append(
                    {
                        "code": "duplicate_stage_order",
                        "pipeline_id": str(pipeline.id),
                        "display_order": order,
                        "count": count,
                    }
                )
        if not any(stage.is_active for stage in stages):
            errors.append(
                {
                    "code": "pipeline_without_active_stage",
                    "pipeline_id": str(pipeline.id),
                    "detail": pipeline.name,
                }
            )

    attribute_names = Counter(
        item.casefold()
        for item in AttributeDefinition.objects.filter(
            organization=organization
        ).values_list("name", flat=True)
    )
    attribute_keys = Counter(
        AttributeDefinition.objects.filter(
            organization=organization
        ).values_list("key", flat=True)
    )
    for name, count in attribute_names.items():
        if count > 1:
            errors.append(
                {"code": "duplicate_attribute_name", "detail": name, "count": count}
            )
    for key, count in attribute_keys.items():
        if count > 1:
            errors.append(
                {"code": "duplicate_attribute_key", "detail": key, "count": count}
            )

    reference_index = _workflow_reference_index(organization)
    for rule in SmartTrigger.objects.filter(organization=organization):
        try:
            _assert_workflow_tenant_references(rule, reference_index)
            clean = validate_workflow_rule(
                organization,
                {
                    "name": rule.name,
                    "enabled": rule.enabled,
                    "trigger_type": rule.trigger_type,
                    "conditions": rule.conditions,
                    "action_type": rule.action_type,
                    "action": rule.action,
                },
            )
            _assert_workflow_safe_attribute_references(
                organization=organization,
                clean=clean,
            )
        except (ValidationError, OperationsPermissionError) as exc:
            errors.append(
                {
                    "code": "invalid_workflow_configuration",
                    "workflow_id": str(rule.id),
                    "detail": sanitize_text(str(exc), limit=300),
                }
            )

    for cycle in _detect_stage_workflow_cycles(organization):
        errors.append(
            {
                "code": "workflow_stage_cycle",
                "stage_path": cycle,
            }
        )

    from apps.integrations.operations_extended_tools import (
        _qualification_public_snapshot,
        _routing_snapshot,
    )
    qualification = _qualification_public_snapshot(organization)
    for item in qualification.get("errors") or []:
        errors.append(
            {
                "code": str(item.get("code") or "qualification_configuration_error"),
                "detail": str(item.get("detail") or ""),
            }
        )
    mapped = set()
    for values in (qualification.get("mappings") or {}).values():
        mapped.update(values)
    for requirement in qualification.get("requirements") or []:
        if requirement.get("required", True) and str(requirement.get("id") or "") not in (
            qualification.get("mappings") or {}
        ):
            warnings.append(
                {
                    "code": "required_qualification_without_attribute_mapping",
                    "requirement_id": str(requirement.get("id") or ""),
                    "detail": str(requirement.get("label") or ""),
                }
            )

    routing = _routing_snapshot(organization)
    for conflict in routing.get("conflicts") or []:
        errors.append(
            {
                "code": "whatsapp_pipeline_routing_conflict",
                **conflict,
            }
        )
    for account in routing.get("accounts") or []:
        if not account.get("valid"):
            warnings.append(
                {
                    "code": "whatsapp_account_not_uniquely_routed",
                    "account_id": account.get("account_id"),
                    "match_count": account.get("match_count"),
                }
            )

    for sequence in FollowupSequence.objects.filter(
        organization=organization
    ).select_related("whatsapp_account").defer("whatsapp_account__access_token"):
        if sequence.whatsapp_account.organization_id != organization.id:
            errors.append(
                {
                    "code": "cadence_cross_tenant_sender",
                    "cadence_id": str(sequence.id),
                }
            )
            continue
        steps = list(sequence.steps.order_by("position"))
        expected = list(range(1, len(steps) + 1))
        positions = [item.position for item in steps]
        if positions != expected:
            warnings.append(
                {
                    "code": "cadence_non_contiguous_positions",
                    "cadence_id": str(sequence.id),
                    "positions": positions[:100],
                }
            )
        if (
            sequence.whatsapp_account.connection_type
            == WhatsAppAccount.ConnectionType.coexisted
        ):
            for step in steps:
                if (
                    step.step_type == FollowupStep.StepType.WHATSAPP
                    and not HostedFollowupStepConfig.objects.filter(step=step).exists()
                ):
                    errors.append(
                        {
                            "code": "hosted_cadence_step_missing_content",
                            "cadence_id": str(sequence.id),
                            "step_id": str(step.id),
                        }
                    )
        else:
            for step in steps:
                if (
                    step.step_type == FollowupStep.StepType.WHATSAPP
                    and (
                        not step.whatsapp_template_id
                        or step.whatsapp_template.account_id
                        != sequence.whatsapp_account_id
                    )
                ):
                    errors.append(
                        {
                            "code": "api_cadence_template_sender_mismatch",
                            "cadence_id": str(sequence.id),
                            "step_id": str(step.id),
                        }
                    )

    info = OrgInfo.objects.filter(organization=organization).first()
    if info and not info.ai_enabled:
        enabled_pipeline_count = Pipeline.objects.filter(
            organization=organization,
            is_active=True,
            ai_enabled=True,
        ).count()
        if enabled_pipeline_count:
            warnings.append(
                {
                    "code": "global_ai_off_with_enabled_pipelines",
                    "pipeline_count": enabled_pipeline_count,
                }
            )
    disabled_pipeline_stage_ai = Stage.objects.filter(
        pipeline__organization=organization,
        pipeline__is_active=True,
        pipeline__ai_enabled=False,
        is_active=True,
        ai_on=True,
    ).count()
    if disabled_pipeline_stage_ai:
        warnings.append(
            {
                "code": "stage_ai_on_under_disabled_pipeline",
                "stage_count": disabled_pipeline_stage_ai,
            }
        )

    duplicate_faqs = (
        FAQ.objects.filter(organization=organization, is_active=True)
        .values("question")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
    )
    for item in duplicate_faqs[:50]:
        warnings.append(
            {
                "code": "duplicate_active_faq_question",
                "detail": str(item["question"])[:300],
                "count": item["count"],
            }
        )

    return ToolExecution(
        data={
            "valid": not errors,
            "etag": configuration_etag(organization),
            "errors": errors[:300],
            "warnings": warnings[:300],
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors_truncated": len(errors) > 300,
            "warnings_truncated": len(warnings) > 300,
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "valid": not errors,
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
    )


def reorder_stages(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_STAGE_CONFIG_WRITE,
        tool_name="reorder_stages",
        arguments=arguments,
    )
    pipeline = Pipeline.objects.filter(
        pk=_uuid((arguments or {}).get("pipeline_id"), field="pipeline_id"),
        organization=organization,
    ).first()
    if pipeline is None:
        raise OperationsToolError("Pipeline not found in this organization.")
    stage_ids = (arguments or {}).get("stage_ids")
    if not isinstance(stage_ids, list) or not stage_ids or not all(
        isinstance(item, str) for item in stage_ids
    ):
        raise OperationsToolError(
            "stage_ids must be a non-empty ordered list of UUID strings."
        )

    current = list(pipeline.stages.order_by("display_order", "name", "id"))
    current_ids = [str(item.id) for item in current]
    if len(stage_ids) != len(current) or set(stage_ids) != set(current_ids):
        raise OperationsToolError(
            "stage_ids must contain every current stage in this pipeline exactly once."
        )
    proposal = {
        "pipeline_id": str(pipeline.id),
        "before": current_ids,
        "after": stage_ids,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "pipeline_id": str(pipeline.id),
                "before": current_ids,
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

    try:
        with transaction.atomic():
            locked_pipeline = Pipeline.objects.select_for_update().get(
                pk=pipeline.pk,
                organization=organization,
            )
            locked = list(
                Stage.objects.select_for_update()
                .filter(pipeline=locked_pipeline)
                .order_by("display_order", "name", "id")
            )
            locked_ids = [str(item.id) for item in locked]
            locked_proposal = {
                "pipeline_id": str(locked_pipeline.id),
                "before": locked_ids,
                "after": stage_ids,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )
            if len(stage_ids) != len(locked) or set(stage_ids) != set(locked_ids):
                raise OperationsApprovalRequired(
                    "The stage list changed after review. Run a fresh dry-run."
                )
            by_id = {str(item.id): item for item in locked}
            offset = max(
                [item.display_order for item in locked] + [0]
            ) + len(locked) + 1000
            for index, item in enumerate(locked, start=1):
                item.display_order = offset + index
            Stage.objects.bulk_update(locked, ["display_order"])
            ordered = []
            for index, stage_id in enumerate(stage_ids, start=1):
                item = by_id[stage_id]
                item.display_order = index
                ordered.append(item)
            Stage.objects.bulk_update(ordered, ["display_order"])
    except IntegrityError as exc:
        raise OperationsToolError(
            "Stage ordering changed concurrently. Run a fresh dry-run."
        ) from exc

    verified = [
        str(item)
        for item in Stage.objects.filter(pipeline=pipeline)
        .order_by("display_order", "name", "id")
        .values_list("id", flat=True)
    ]
    if verified != stage_ids:
        raise OperationsToolError("Stage reorder verification failed.")
    return ToolExecution(
        data={
            "status": "FIXED",
            "pipeline_id": str(pipeline.id),
            "stage_ids": verified,
            "etag": configuration_etag(organization),
            "verification": "passed",
        },
        capability=CAP_STAGE_CONFIG_WRITE,
        target_type="pipeline",
        target_id=str(pipeline.id),
        reason=reason,
        audit_summary={
            "operation": "reorder_stages",
            "verification": "passed",
        },
    )


CONFIGURATION_STATE_HANDLERS = {
    "get_configuration_dependency_graph": get_configuration_dependency_graph,
    "validate_organization_configuration": validate_organization_configuration,
    "export_organization_configuration": export_organization_configuration,
    "reorder_stages": reorder_stages,
}
