# ruff: noqa: F401
"""Approval-plan creation, application, and rollback for Operations MCP configuration."""

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
    _json_hash,
    configuration_etag,
)
from apps.integrations.operations.configuration.validation import reorder_stages

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
