# ruff: noqa: F401
"""Portable configuration import planning for Operations MCP."""

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
    _account_ref,
    export_organization_configuration,
)
from apps.integrations.operations.configuration.validation import (
    get_configuration_dependency_graph,
    reorder_stages,
    validate_organization_configuration,
)
from apps.integrations.operations.configuration.plans import (
    apply_configuration_plan,
    create_configuration_plan,
    rollback_configuration_plan,
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
        provider = str(cadence.get("provider") or "api").strip()
        account_ref = str(cadence.get("account_ref") or "").strip()
        cadence_data = {
            "name": name,
            "description": str(cadence.get("description") or ""),
            "provider": provider,
        }
        if account_ref:
            account_id = account_refs.get(account_ref)
            if not account_id:
                raise OperationsPermissionError(
                    f"Cadence '{name}' references an unavailable WhatsApp account."
                )
            cadence_data["whatsapp_account_id"] = account_id
        elif provider == "api":
            raise OperationsPermissionError(
                f"WhatsApp API Cadence '{name}' requires an account_ref."
            )
        operations.append(
            {
                "ref": ref,
                "tool": "upsert_cadence_configuration",
                "arguments": {
                    **({"cadence_id": str(existing.id)} if existing else {}),
                    "data": cadence_data,
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
