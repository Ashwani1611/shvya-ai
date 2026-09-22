"""Extended configuration tools for the actor-bound SHVYA Operations MCP.

This module is imported lazily by operations_tools so the existing Operations
security boundary remains authoritative.  It exposes existing domain services
through dry-run/approval/audit aware adapters; it does not create a second CRM,
qualification, messaging, workflow, Cadence, FAQ, or knowledge runtime.
"""

from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.utils import timezone

from apps.ai_engagement.models import Document, FAQ, KnowledgeSource, OrgInfo
from apps.ai_engagement.services.faq import FAQService, FAQServiceError
from apps.ai_engagement.services.knowledge import (
    KnowledgeExtractionError,
    KnowledgeIngestionService,
)
from apps.ai_engagement.services.knowledge_source import (
    KnowledgeSourceService,
    KnowledgeSourceServiceError,
)
from apps.ai_engagement.services.playbook import (
    SECTION_TITLES,
    evaluate_playbook_criteria,
    parse_playbook,
    validate_playbook,
)
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.models import AttributeDefinition, Pipeline, Stage
from apps.followups.models import FollowupExecution, FollowupSequence, FollowupStep
from apps.followups.touchpoint_models import TouchpointCategory, TouchpointReply
from apps.hosted_automation.models import HostedFollowupStepConfig
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    approval_required,
)
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_automation_service import (
    HostedAutomationError,
    add_hosted_whatsapp_step as domain_add_hosted_whatsapp_step,
    update_hosted_whatsapp_step as domain_update_hosted_whatsapp_step,
)
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    create_hosted_account,
    get_pipeline_for_account,
    get_session_settings,
    normalize_whatsapp_number,
    pipeline_whatsapp_number,
    require_pipeline_number,
)
from services.followup_service import (
    FollowupError,
    _move_into_business_hours,
    _recalculate_active_states,
    _validate_schedule,
    calculate_step_due,
    delete_step,
)
from services.triggers.evaluator import matches as workflow_matches
from services.triggers.rules import (
    catalog as workflow_catalog,
    validate as validate_workflow_rule,
)

from apps.integrations.operations_tools import (
    OperationsApprovalRequired,
    OperationsPermissionError,
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _lead,
    _organization_for,
    _proposal_digest,
    _reject_secret_like_content,
    _require_operations_capability,
    _sensitive_attribute_keys,
    _uuid,
    _write_gate,
    _cadence_schedule,
    _assert_workflow_safe_attribute_references,
)


# The Operations JSON-RPC endpoint intentionally keeps a 1 MiB request cap.
# Base64 expands bytes by roughly one third, so MCP file ingestion is bounded
# below that transport limit. Larger knowledge files continue through the
# existing authenticated dashboard upload path.
MAX_MCP_KNOWLEDGE_UPLOAD_BYTES = 512 * 1024


def _safe_requirements_for_org(organization):
    from apps.ai_engagement.services import organization_profile as profile_module

    info = OrgInfo.objects.filter(organization=organization).first()
    raw = str(getattr(info, "ai_playbook", "") or "")
    sections = parse_playbook(raw)
    compiled = profile_module.compile_qualification_requirements(
        sections["qualification_questions"]
    )
    from apps.ai_engagement.services.qualification_execution_contract import _config

    config = _config(
        organization=organization,
        requirements=compiled.get("requirements", []),
    )
    return raw, sections, compiled, config


def _qualification_public_snapshot(organization):
    raw, sections, compiled, config = _safe_requirements_for_org(organization)
    return {
        "configured": bool(raw.strip()),
        "mode": compiled.get("mode"),
        "flow_version": compiled.get("flow_version"),
        "requirements": deepcopy(compiled.get("requirements", [])),
        "criteria": [
            line.strip()
            for line in str(sections.get("qualification_criteria") or "").splitlines()
            if line.strip()
        ],
        "mappings": deepcopy(config.get("mapping_targets") or {}),
        "completion_stage": (
            {
                "id": str(config["completion_stage"]["id"]),
                "name": config["completion_stage"]["name"],
                "pipeline_id": str(config["completion_stage"]["pipeline_id"]),
                "pipeline_name": config["completion_stage"]["pipeline__name"],
            }
            if config.get("completion_stage")
            else None
        ),
        "final_ack": config.get("final_ack") or "",
        "reminder_rules": list(config.get("reminder_rules") or []),
        "errors": deepcopy(config.get("errors") or []),
    }


def get_qualification_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    snapshot = _qualification_public_snapshot(organization)
    return ToolExecution(
        data=snapshot,
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "requirement_count": len(snapshot["requirements"]),
            "configuration_error_count": len(snapshot["errors"]),
            "flow_version": snapshot["flow_version"] or "",
        },
    )


def _clean_qualification_data(*, organization, data):
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a qualification configuration object.")

    requirements = data.get("requirements")
    if not isinstance(requirements, list) or not requirements or len(requirements) > 30:
        raise OperationsToolError("Qualification requires between 1 and 30 requirements.")

    mode = str(data.get("mode") or "configured").strip()
    if mode not in {"configured", "all_required", "majority"}:
        raise OperationsToolError("mode must be configured, all_required, or majority.")

    seen_ids = set()
    cleaned_requirements = []
    for index, item in enumerate(requirements, start=1):
        if not isinstance(item, dict):
            raise OperationsToolError("Each qualification requirement must be an object.")
        stable_id = str(item.get("stable_id") or f"qualification_{index}").strip()
        stable_id = re.sub(r"[^A-Za-z0-9_-]+", "_", stable_id).strip("_")[:64]
        if not stable_id:
            raise OperationsToolError("Each qualification requirement needs a stable_id.")
        if stable_id in seen_ids:
            raise OperationsToolError(f"Duplicate qualification stable_id: {stable_id}.")
        seen_ids.add(stable_id)

        question = str(item.get("question") or "").strip()
        if not question or len(question) > 2000:
            raise OperationsToolError("Qualification question must be 1-2000 characters.")

        required = item.get("required", True)
        if not isinstance(required, bool):
            raise OperationsToolError("Qualification required must be a boolean.")

        options = item.get("options") or []
        if not isinstance(options, list) or len(options) > 30:
            raise OperationsToolError("Qualification options must be a list of at most 30 items.")
        clean_options = []
        option_keys = set()
        for option_index, option in enumerate(options, start=1):
            if isinstance(option, str):
                key = chr(64 + option_index) if option_index <= 26 else str(option_index)
                value = option.strip()
            elif isinstance(option, dict):
                key = str(option.get("key") or "").strip().upper()
                value = str(option.get("value") or "").strip()
            else:
                raise OperationsToolError("Each qualification option must be text or an object.")
            if not key or not value or len(key) > 10 or len(value) > 500:
                raise OperationsToolError("Qualification option key/value is invalid.")
            if key in option_keys:
                raise OperationsToolError(f"Duplicate qualification option key: {key}.")
            option_keys.add(key)
            clean_options.append({"key": key, "value": value})

        condition = item.get("eligible_when")
        clean_condition = None
        if condition is not None:
            if not isinstance(condition, dict):
                raise OperationsToolError("eligible_when must be an object.")
            source = str(condition.get("requirement_id") or "").strip()
            operator = str(condition.get("operator") or "eq").strip().lower()
            if operator != "eq":
                raise OperationsToolError("Qualification eligible_when currently supports only eq.")
            if source not in seen_ids:
                raise OperationsToolError(
                    "A conditional qualification requirement may reference only an earlier stable_id."
                )
            value = condition.get("value")
            if not isinstance(value, (str, int, float, bool)) or value is None:
                raise OperationsToolError("eligible_when.value must be a scalar value.")
            clean_condition = {
                "requirement_id": source,
                "operator": "eq",
                "value": value,
            }

        cleaned_requirements.append(
            {
                "stable_id": stable_id,
                "question": question,
                "required": required,
                "options": clean_options,
                "eligible_when": clean_condition,
            }
        )

    criteria = data.get("criteria")
    if criteria is None:
        criteria = ["All required qualification questions are answered"]
    if not isinstance(criteria, list) or not criteria or len(criteria) > 30:
        raise OperationsToolError("Qualification criteria must be a non-empty list.")
    clean_criteria = []
    for item in criteria:
        value = str(item or "").strip()
        if not value or len(value) > 2000:
            raise OperationsToolError("Each qualification criterion must be 1-2000 characters.")
        clean_criteria.append(value)

    mapping_rows = data.get("mappings") or []
    if isinstance(mapping_rows, dict):
        mapping_rows = [
            {"requirement_id": key, "attribute_keys": value}
            for key, value in mapping_rows.items()
        ]
    if not isinstance(mapping_rows, list) or len(mapping_rows) > 60:
        raise OperationsToolError("mappings must be a list or object.")

    sensitive = _sensitive_attribute_keys(organization)
    attributes = {
        item.key: item
        for item in AttributeDefinition.objects.filter(
            organization=organization,
            is_active=True,
        )
        if item.key not in sensitive
    }
    requirement_ids = {item["stable_id"] for item in cleaned_requirements}
    clean_mappings = []
    for row in mapping_rows:
        if not isinstance(row, dict):
            raise OperationsToolError("Each qualification mapping must be an object.")
        requirement_id = str(row.get("requirement_id") or "").strip()
        if requirement_id not in requirement_ids:
            raise OperationsToolError(
                f"Unknown qualification mapping requirement: {requirement_id}."
            )
        keys = row.get("attribute_keys") or []
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list) or not keys or len(keys) > 10:
            raise OperationsToolError("Each qualification mapping needs 1-10 attribute_keys.")
        clean_keys = []
        for key in keys:
            key = str(key or "").strip()
            if key not in attributes:
                raise OperationsToolError(
                    f"Qualification mapping attribute is missing, foreign, or sensitive: {key}."
                )
            if key not in clean_keys:
                clean_keys.append(key)
        clean_mappings.append(
            {"requirement_id": requirement_id, "attribute_keys": clean_keys}
        )

    target_stage_id = str(data.get("target_stage_id") or "").strip()
    if not target_stage_id:
        raise OperationsToolError("target_stage_id is required.")
    target_stage = (
        Stage.objects.select_related("pipeline")
        .filter(
            pk=_uuid(target_stage_id, field="target_stage_id"),
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        )
        .first()
    )
    if target_stage is None:
        raise OperationsToolError("Qualification target stage must be active in this organization.")

    final_ack = str(data.get("final_ack") or "").strip()
    if not final_ack or len(final_ack) > 2000:
        raise OperationsToolError("final_ack must be 1-2000 characters.")

    return {
        "mode": mode,
        "requirements": cleaned_requirements,
        "criteria": clean_criteria,
        "mappings": clean_mappings,
        "target_stage": target_stage,
        "final_ack": final_ack,
        "attributes": attributes,
    }


def _qualification_value_text(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _render_qualification_playbook(*, organization, data, existing_raw):
    clean = _clean_qualification_data(organization=organization, data=data)
    sections = parse_playbook(existing_raw)

    question_lines = []
    if clean["mode"] == "all_required":
        question_lines.append("All questions are required.")
    elif clean["mode"] == "majority":
        question_lines.append("Majority of qualification requirements are required.")

    stable_to_q = {}
    for index, item in enumerate(clean["requirements"], start=1):
        stable_to_q[item["stable_id"]] = index
        question = item["question"].splitlines()[0].strip()
        if not item["required"]:
            question = question.rstrip() + " (optional)"
        condition = item.get("eligible_when")
        if condition:
            question = (
                f"[if: {condition['requirement_id']} = "
                f"{_qualification_value_text(condition['value'])}] {question}"
            )
        question_lines.append(f"{index}. [id: {item['stable_id']}] {question}")
        for option in item["options"]:
            question_lines.append(f"   {option['key']}. {option['value']}")

    mapping_lines = []
    for row in clean["mappings"]:
        q_number = stable_to_q[row["requirement_id"]]
        names = [clean["attributes"][key].name for key in row["attribute_keys"]]
        mapping_lines.append(f"Q{q_number} -> " + " + ".join(names))

    target = clean["target_stage"]
    stage_line = (
        "When all required qualification questions are answered and qualification "
        f"criteria are satisfied, move the lead to {target.name} in {target.pipeline.name}."
    )

    replacements = {
        "qualification_questions": "\n".join(question_lines),
        "acknowledgment_message": clean["final_ack"],
        "qualification_criteria": "\n".join(
            f"{index}. {value}"
            for index, value in enumerate(clean["criteria"], start=1)
        ),
        "stage_shifting": stage_line,
        "attribute_mapped": "\n".join(mapping_lines),
    }

    rendered = []
    for key, title in SECTION_TITLES.items():
        value = replacements.get(key, sections.get(key, ""))
        value = str(value or "").strip()
        if value:
            rendered.append(f"## {title}\n{value}")
    candidate = validate_playbook("\n\n".join(rendered))

    from apps.ai_engagement.services import organization_profile as profile_module
    from apps.ai_engagement.services.qualification_execution_contract import _config

    candidate_sections = parse_playbook(candidate)
    compiled = profile_module.compile_qualification_requirements(
        candidate_sections["qualification_questions"]
    )
    requirements = list(compiled.get("requirements") or [])
    if len(requirements) != len(clean["requirements"]):
        raise OperationsToolError(
            "Qualification compilation changed the number of authored requirements."
        )
    expected_stable = [item["stable_id"] for item in clean["requirements"]]
    actual_stable = [str(item.get("stable_id") or "") for item in requirements]
    if actual_stable != expected_stable:
        raise OperationsToolError("Qualification stable IDs did not compile deterministically.")

    config = _config(
        organization=organization,
        requirements=requirements,
        raw_override=candidate,
    )
    errors = list(config.get("errors") or [])
    if errors:
        codes = ", ".join(sorted({str(item.get("code") or "configuration_error") for item in errors}))
        raise OperationsToolError(f"Qualification configuration is invalid: {codes}.")

    completion = config.get("completion_stage")
    if not completion or str(completion.get("id")) != str(target.id):
        raise OperationsToolError(
            "Qualification completion target did not compile to the selected stage."
        )

    return {
        "playbook": candidate,
        "compiled": compiled,
        "config": config,
        "clean": clean,
    }


def validate_qualification_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    info = OrgInfo.objects.filter(organization=organization).first()
    existing_raw = str(getattr(info, "ai_playbook", "") or "")
    result = _render_qualification_playbook(
        organization=organization,
        data=(arguments or {}).get("data"),
        existing_raw=existing_raw,
    )
    config = result["config"]
    return ToolExecution(
        data={
            "valid": True,
            "mode": result["compiled"].get("mode"),
            "flow_version": result["compiled"].get("flow_version"),
            "requirement_count": len(result["compiled"].get("requirements") or []),
            "mapping_count": sum(
                len(value) for value in (config.get("mapping_targets") or {}).values()
            ),
            "completion_stage": {
                "id": str(config["completion_stage"]["id"]),
                "name": config["completion_stage"]["name"],
                "pipeline_id": str(config["completion_stage"]["pipeline_id"]),
                "pipeline_name": config["completion_stage"]["pipeline__name"],
            },
            "configuration_errors": [],
            "side_effects": False,
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "valid": True,
            "requirement_count": len(result["compiled"].get("requirements") or []),
            "flow_version": result["compiled"].get("flow_version") or "",
        },
    )


def upsert_qualification_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="upsert_qualification_configuration",
        arguments=arguments,
    )
    info = OrgInfo.objects.filter(organization=organization).first()
    before = str(getattr(info, "ai_playbook", "") or "")
    result = _render_qualification_playbook(
        organization=organization,
        data=(arguments or {}).get("data"),
        existing_raw=before,
    )
    after = result["playbook"]
    proposal = {
        "organization_id": str(organization.id),
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "changed": before != after,
                "flow_version": result["compiled"].get("flow_version"),
                "requirement_count": len(result["compiled"].get("requirements") or []),
                "target_stage_id": str(result["clean"]["target_stage"].id),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="organization",
            target_id=str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_qualification_configuration",
                "proposal_digest": _proposal_digest(proposal),
                "requirement_count": len(result["compiled"].get("requirements") or []),
            },
        )

    with transaction.atomic():
        locked_organization = organization.__class__.objects.select_for_update().get(
            pk=organization.pk
        )
        locked_info = (
            OrgInfo.objects.select_for_update()
            .filter(organization=locked_organization)
            .first()
        )
        locked_before = str(getattr(locked_info, "ai_playbook", "") or "")
        locked_result = _render_qualification_playbook(
            organization=locked_organization,
            data=(arguments or {}).get("data"),
            existing_raw=locked_before,
        )
        locked_after = locked_result["playbook"]
        locked_proposal = {
            "organization_id": str(locked_organization.id),
            "before": locked_before,
            "after": locked_after,
        }
        _ensure_approved_proposal_unchanged(
            arguments=arguments,
            proposal=locked_proposal,
        )
        if locked_info is None:
            locked_info = OrgInfo(organization=locked_organization)
        locked_info.ai_playbook = locked_after
        locked_info.full_clean()
        locked_info.save()
        verify = _qualification_public_snapshot(locked_organization)
        if (
            verify.get("flow_version") != locked_result["compiled"].get("flow_version")
            or verify.get("errors")
        ):
            raise OperationsToolError("Qualification configuration verification failed.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "flow_version": verify["flow_version"],
            "requirement_count": len(verify["requirements"]),
            "completion_stage": verify["completion_stage"],
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="organization",
        target_id=str(organization.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_qualification_configuration",
            "flow_version": verify["flow_version"] or "",
            "verification": "passed",
        },
    )


def list_whatsapp_accounts(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    accounts = list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            is_active=True,
        )
        .defer("access_token")
        .order_by("connection_type", "business_name", "id")[:100]
    )
    rows = []
    for account in accounts:
        pipeline = get_pipeline_for_account(account=account)
        rows.append(
            {
                "id": str(account.id),
                "connection_type": account.connection_type,
                "business_name": account.business_name,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
                "pipeline": (
                    {"id": str(pipeline.id), "name": pipeline.name}
                    if pipeline else None
                ),
                "routing_valid": pipeline is not None,
            }
        )
    return ToolExecution(
        data={"accounts": rows, "count": len(rows)},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"account_count": len(rows)},
    )


def _routing_snapshot(organization):
    pipelines = list(
        Pipeline.objects.filter(organization=organization, is_active=True)
        .only("id", "name", "country_code", "phone_number")
        .order_by("name")
    )
    by_number = {}
    for pipeline in pipelines:
        number = pipeline_whatsapp_number(pipeline)
        if number:
            by_number.setdefault(number, []).append(pipeline)

    conflicts = [
        {
            "phone_number": number,
            "pipeline_ids": [str(item.id) for item in values],
            "pipeline_names": [item.name for item in values],
        }
        for number, values in by_number.items()
        if len(values) > 1
    ]
    accounts = list(
        WhatsAppAccount.objects.filter(organization=organization, is_active=True)
        .defer("access_token")
        .order_by("id")
    )
    account_rows = []
    for account in accounts:
        number = normalize_whatsapp_number(
            phone_number=account.display_phone_number or account.phone_number_id
        )
        matches = by_number.get(number, [])
        account_rows.append(
            {
                "account_id": str(account.id),
                "connection_type": account.connection_type,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
                "pipeline_id": str(matches[0].id) if len(matches) == 1 else None,
                "pipeline_name": matches[0].name if len(matches) == 1 else None,
                "match_count": len(matches),
                "valid": len(matches) == 1,
            }
        )
    return {
        "valid": not conflicts and all(row["valid"] for row in account_rows),
        "conflicts": conflicts,
        "accounts": account_rows,
        "active_pipeline_count": len(pipelines),
    }


def validate_whatsapp_routing(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    snapshot = _routing_snapshot(organization)
    return ToolExecution(
        data=snapshot,
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "valid": snapshot["valid"],
            "conflict_count": len(snapshot["conflicts"]),
            "account_count": len(snapshot["accounts"]),
        },
    )


def bind_whatsapp_account_to_pipeline(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_MESSAGING_CONFIG_WRITE,
        tool_name="bind_whatsapp_account_to_pipeline",
        arguments=arguments,
    )
    pipeline = (
        Pipeline.objects.filter(
            pk=_uuid((arguments or {}).get("pipeline_id"), field="pipeline_id"),
            organization=organization,
            is_active=True,
        ).first()
    )
    if pipeline is None:
        raise OperationsToolError("Active pipeline not found in this organization.")

    account_id = str((arguments or {}).get("whatsapp_account_id") or "").strip()
    account = None
    if account_id:
        account = (
            WhatsAppAccount.objects.filter(
                pk=_uuid(account_id, field="whatsapp_account_id"),
                organization=organization,
                is_active=True,
            )
            .defer("access_token")
            .first()
        )
        if account is None:
            raise OperationsToolError("Active WhatsApp account not found in this organization.")

    country_code = str((arguments or {}).get("country_code") or pipeline.country_code or "").strip()
    phone_number = str((arguments or {}).get("phone_number") or "").strip()
    if account is not None and not phone_number:
        phone_number = account.display_phone_number or account.phone_number_id

    normalized = normalize_whatsapp_number(
        country_code=country_code,
        phone_number=phone_number,
    )
    if not normalized:
        raise OperationsToolError("A valid WhatsApp phone number including country code is required.")

    if account is not None:
        account_number = normalize_whatsapp_number(
            phone_number=account.display_phone_number or account.phone_number_id
        )
        if account_number and account_number != normalized:
            raise OperationsToolError(
                "The requested pipeline number does not match the selected WhatsApp account."
            )

    conflicts = [
        item
        for item in Pipeline.objects.filter(
            organization=organization,
            is_active=True,
        ).exclude(pk=pipeline.pk)
        if pipeline_whatsapp_number(item) == normalized
    ]
    if conflicts:
        raise OperationsToolError(
            "This WhatsApp number is already mapped to another active pipeline."
        )

    before = {
        "country_code": pipeline.country_code,
        "phone_number": pipeline.phone_number,
    }
    after = {
        "country_code": country_code,
        "phone_number": normalized,
    }
    proposal = {
        "pipeline_id": str(pipeline.id),
        "whatsapp_account_id": str(account.id) if account else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "pipeline_id": str(pipeline.id),
                "display_phone_number": normalized,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_MESSAGING_CONFIG_WRITE,
                ),
                "routing_conflicts": 0,
                "reversible": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="pipeline",
            target_id=str(pipeline.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "bind_whatsapp_account_to_pipeline",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    with transaction.atomic():
        locked = (
            Pipeline.objects.select_for_update()
            .filter(pk=pipeline.pk, organization=organization, is_active=True)
            .first()
        )
        if locked is None:
            raise OperationsApprovalRequired(
                "The pipeline changed or became inactive. Run a fresh dry-run."
            )
        locked_before = {
            "country_code": locked.country_code,
            "phone_number": locked.phone_number,
        }
        locked_proposal = {
            "pipeline_id": str(locked.id),
            "whatsapp_account_id": str(account.id) if account else None,
            "before": locked_before,
            "after": after,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        for item in Pipeline.objects.select_for_update().filter(
            organization=organization, is_active=True
        ).exclude(pk=locked.pk):
            if pipeline_whatsapp_number(item) == normalized:
                raise OperationsApprovalRequired(
                    "WhatsApp routing changed after review. Run a fresh dry-run."
                )
        locked.country_code = country_code
        locked.phone_number = normalized
        locked.save(update_fields=["country_code", "phone_number", "updated_at"])
        if pipeline_whatsapp_number(locked) != normalized:
            raise OperationsToolError("WhatsApp pipeline binding verification failed.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "pipeline_id": str(pipeline.id),
            "display_phone_number": normalized,
            "verification": "passed",
        },
        capability=CAP_MESSAGING_CONFIG_WRITE,
        target_type="pipeline",
        target_id=str(pipeline.id),
        reason=reason,
        audit_summary={
            "operation": "bind_whatsapp_account_to_pipeline",
            "verification": "passed",
        },
    )


def begin_whatsapp_connection(*, identity, arguments):
    organization = _organization_for(identity)
    if not is_hosted_account_enabled(organization):
        raise OperationsPermissionError(
            "Hosted WhatsApp is not enabled for this organization."
        )
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_MESSAGING_CONFIG_WRITE,
        tool_name="begin_whatsapp_connection",
        arguments=arguments,
    )
    country_code = str((arguments or {}).get("country_code") or "").strip()
    phone_number = str((arguments or {}).get("phone_number") or "").strip()
    try:
        normalized, pipeline = require_pipeline_number(
            organization=organization,
            country_code=country_code,
            phone_number=phone_number,
        )
    except HostedWhatsAppValidationError as exc:
        raise OperationsToolError(str(exc)) from exc

    existing = (
        WhatsAppAccount.objects.filter(
            organization=organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            display_phone_number=normalized,
            is_active=True,
        )
        .defer("access_token")
        .first()
    )
    proposal = {
        "organization_id": str(organization.id),
        "pipeline_id": str(pipeline.id),
        "display_phone_number": normalized,
        "existing_account_id": str(existing.id) if existing else None,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "pipeline": {"id": str(pipeline.id), "name": pipeline.name},
                "display_phone_number": normalized,
                "existing_account": bool(existing),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_MESSAGING_CONFIG_WRITE,
                ),
                "requires_human_qr_scan": True,
                "safe_status_only": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="pipeline",
            target_id=str(pipeline.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "begin_whatsapp_connection",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    from apps.channels.hosted_tasks import initialize_hosted_session_task

    with transaction.atomic():
        locked_organization = organization.__class__.objects.select_for_update().get(
            pk=organization.pk
        )
        try:
            normalized_locked, pipeline_locked = require_pipeline_number(
                organization=locked_organization,
                country_code=country_code,
                phone_number=phone_number,
            )
        except HostedWhatsAppValidationError as exc:
            raise OperationsApprovalRequired(
                "Hosted WhatsApp routing changed after review. Run a fresh dry-run."
            ) from exc
        existing_locked = (
            WhatsAppAccount.objects.select_for_update()
            .filter(
                organization=locked_organization,
                connection_type=WhatsAppAccount.ConnectionType.coexisted,
                display_phone_number=normalized_locked,
                is_active=True,
            )
            .defer("access_token")
            .first()
        )
        locked_proposal = {
            "organization_id": str(locked_organization.id),
            "pipeline_id": str(pipeline_locked.id),
            "display_phone_number": normalized_locked,
            "existing_account_id": str(existing_locked.id) if existing_locked else None,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        try:
            account, pipeline_locked, created = create_hosted_account(
                organization=locked_organization,
                created_by=identity.actor,
                country_code=country_code,
                phone_number=phone_number,
            )
        except HostedWhatsAppValidationError as exc:
            raise OperationsToolError(str(exc)) from exc
        transaction.on_commit(
            lambda account_id=str(account.id): initialize_hosted_session_task.delay(account_id)
        )

    account.refresh_from_db()
    return ToolExecution(
        data={
            "status": "FIXED",
            "created": created,
            "account": {
                "id": str(account.id),
                "connection_type": account.connection_type,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
            },
            "pipeline": {"id": str(pipeline_locked.id), "name": pipeline_locked.name},
            "requires_human_qr_scan": account.status != WhatsAppAccount.Status.CONNECTED,
            "safe_status_only": True,
            "verification": "passed",
        },
        capability=CAP_MESSAGING_CONFIG_WRITE,
        target_type="whatsapp_account",
        target_id=str(account.id),
        reason=reason,
        audit_summary={
            "operation": "begin_whatsapp_connection",
            "created": created,
            "pipeline_id": str(pipeline_locked.id),
            "verification": "passed",
        },
    )


def _workflow_catalog_safe(organization):
    data = workflow_catalog(organization)
    sensitive = _sensitive_attribute_keys(organization)
    data["attributes"] = [
        item for item in data.get("attributes", [])
        if item.get("key") not in sensitive
    ]
    return data


def _trigger_schema(trigger_type):
    properties = {
        "scopes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pipeline": {"type": "string", "format": "uuid"},
                    "stages": {
                        "type": "array",
                        "items": {"type": "string", "format": "uuid"},
                        "minItems": 1,
                    },
                },
                "required": ["pipeline", "stages"],
                "additionalProperties": False,
            },
        },
        "sources": {"type": "array", "items": {"type": "string"}},
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "match": {"type": "string", "enum": ["equals", "contains"]},
                    "values": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["key", "match", "values"],
                "additionalProperties": False,
            },
        },
    }
    required = [] if trigger_type == "sequence_ended" else ["scopes"]
    if trigger_type in {"no_response", "stage_idle"}:
        properties.update(
            {
                "duration": {"type": "integer", "minimum": 1, "maximum": 525600},
                "unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
            }
        )
        required += ["duration", "unit"]
    elif trigger_type == "keyword":
        properties["keywords"] = {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
        }
        required += ["keywords"]
    elif trigger_type == "sequence_ended":
        properties["sequences"] = {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "minItems": 1,
            "maxItems": 100,
        }
        required += ["sequences"]
    elif trigger_type == "call_logged":
        properties["call_status"] = {"type": "string"}
        required += ["call_status"]
    elif trigger_type == "call_intelligence_ready":
        properties["intent"] = {
            "type": "string",
            "enum": ["any", "unknown", "low", "medium", "high"],
        }
        properties["min_ai_score"] = {"type": "integer", "minimum": 0, "maximum": 10}
        required += ["intent", "min_ai_score"]
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(set(required)),
        "additionalProperties": False,
    }


def _action_schema(action_type):
    schemas = {
        "start_sequence": {
            "sequence": {"type": "string", "format": "uuid"},
            "replace": {"type": "boolean"},
        },
        "move_stage": {
            "pipeline": {"type": "string", "format": "uuid"},
            "stage": {"type": "string", "format": "uuid"},
        },
        "message": {
            "body": {"type": "string", "maxLength": 20000},
            "account": {"type": "string", "format": "uuid"},
            "schedule": {"type": "string", "enum": ["relative", "fixed", "attribute"]},
            "duration": {"type": "integer", "minimum": 0, "maximum": 525600},
            "unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
            "time": {"type": "string", "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"},
            "date_attribute": {"type": "string"},
        },
        "email": {
            "subject": {"type": "string", "maxLength": 255},
            "body": {"type": "string", "maxLength": 20000},
        },
        "reminder": {
            "duration": {"type": "integer", "minimum": 0, "maximum": 525600},
            "unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
            "note": {"type": "string", "maxLength": 2000},
            "overwrite": {"type": "boolean"},
        },
        "attribute": {
            "key": {"type": "string"},
            "value": {"type": ["string", "number"]},
        },
        "stop_sequence": {},
        "ai": {"enabled": {"type": "boolean"}},
        "followup": {"enabled": {"type": "boolean"}},
    }
    properties = schemas[action_type]
    required_map = {
        "start_sequence": ["sequence", "replace"],
        "move_stage": ["pipeline", "stage"],
        "message": ["body", "account", "schedule"],
        "email": ["subject", "body"],
        "reminder": ["duration", "unit"],
        "attribute": ["key", "value"],
        "stop_sequence": [],
        "ai": ["enabled"],
        "followup": ["enabled"],
    }
    return {
        "type": "object",
        "properties": properties,
        "required": required_map[action_type],
        "additionalProperties": False,
    }


def list_workflow_triggers(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    catalog = _workflow_catalog_safe(organization)
    rows = [
        {"type": key, "label": value, "conditions_schema": _trigger_schema(key)}
        for key, value in catalog["triggers"].items()
    ]
    return ToolExecution(
        data={"triggers": rows},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"trigger_type_count": len(rows)},
    )


def list_workflow_actions(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    catalog = _workflow_catalog_safe(organization)
    rows = [
        {"type": key, "label": value, "action_schema": _action_schema(key)}
        for key, value in catalog["actions"].items()
    ]
    return ToolExecution(
        data={"actions": rows},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"action_type_count": len(rows)},
    )


def get_workflow_schema(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    catalog = _workflow_catalog_safe(organization)
    trigger_type = str((arguments or {}).get("trigger_type") or "").strip()
    action_type = str((arguments or {}).get("action_type") or "").strip()
    if trigger_type and trigger_type not in catalog["triggers"]:
        raise OperationsToolError("Unknown Workflow trigger_type.")
    if action_type and action_type not in catalog["actions"]:
        raise OperationsToolError("Unknown Workflow action_type.")

    trigger_schemas = (
        {trigger_type: _trigger_schema(trigger_type)}
        if trigger_type
        else {key: _trigger_schema(key) for key in catalog["triggers"]}
    )
    action_schemas = (
        {action_type: _action_schema(action_type)}
        if action_type
        else {key: _action_schema(key) for key in catalog["actions"]}
    )
    return ToolExecution(
        data={
            "trigger_schemas": trigger_schemas,
            "action_schemas": action_schemas,
            "catalog": {
                "sources": catalog["sources"],
                "pipelines": catalog["pipelines"],
                "stages": catalog["stages"],
                "sequences": catalog["sequences"],
                "attributes": catalog["attributes"],
                "accounts": catalog["accounts"],
                "call_statuses": catalog["call_statuses"],
                "call_intents": catalog["call_intents"],
                "timezone": catalog["timezone"],
            },
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "trigger_schema_count": len(trigger_schemas),
            "action_schema_count": len(action_schemas),
        },
    )


def validate_workflow_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    data = (arguments or {}).get("data")
    _reject_secret_like_content(data, field="workflow")
    try:
        clean = validate_workflow_rule(organization, data)
    except ValidationError as exc:
        raise OperationsToolError("Workflow validation failed: " + "; ".join(exc.messages)) from exc
    _assert_workflow_safe_attribute_references(
        organization=organization,
        clean=clean,
    )
    return ToolExecution(
        data={
            "valid": True,
            "normalized": clean,
            "side_effects": False,
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "valid": True,
            "trigger_type": clean["trigger_type"],
            "action_type": clean["action_type"],
        },
    )


def list_touchpoints(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    include_archived = bool((arguments or {}).get("include_archived", False))
    reply_qs = TouchpointReply.objects.all()
    if not include_archived:
        reply_qs = reply_qs.filter(is_active=True)
    categories = list(
        TouchpointCategory.objects.filter(organization=organization)
        .prefetch_related(Prefetch("replies", queryset=reply_qs))
        .order_by("name")[:100]
    )
    rows = [
        {
            "category_id": str(category.id),
            "category_name": category.name,
            "replies": [
                {
                    "id": str(reply.id),
                    "title": reply.title,
                    "body": reply.body,
                    "active": reply.is_active,
                    "updated_at": reply.updated_at.isoformat(),
                }
                for reply in category.replies.all()
            ],
        }
        for category in categories
    ]
    return ToolExecution(
        data={"categories": rows, "count": sum(len(row["replies"]) for row in rows)},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"touchpoint_count": sum(len(row["replies"]) for row in rows)},
    )


def _touchpoint_proposal(*, organization, arguments):
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Touchpoint object.")
    _reject_secret_like_content(data, field="touchpoint")
    touchpoint_id = str((arguments or {}).get("touchpoint_id") or "").strip()
    reply = None
    if touchpoint_id:
        reply = (
            TouchpointReply.objects.select_related("category")
            .filter(
                pk=_uuid(touchpoint_id, field="touchpoint_id"),
                category__organization=organization,
            )
            .first()
        )
        if reply is None:
            raise OperationsToolError("Touchpoint not found in this organization.")

    category_id = str(data.get("category_id") or "").strip()
    category_name = str(data.get("category_name") or "").strip()
    category = None
    if category_id:
        category = TouchpointCategory.objects.filter(
            pk=_uuid(category_id, field="category_id"),
            organization=organization,
        ).first()
        if category is None:
            raise OperationsToolError("Touchpoint category not found in this organization.")
    elif category_name:
        category = TouchpointCategory.objects.filter(
            organization=organization,
            name__iexact=category_name,
        ).first()
    elif reply:
        category = reply.category
    else:
        raise OperationsToolError("category_id or category_name is required.")

    title = str(data.get("title", reply.title if reply else "") or "").strip()
    body = str(data.get("body", reply.body if reply else "") or "").strip()
    if not title or len(title) > 150:
        raise OperationsToolError("Touchpoint title must be 1-150 characters.")
    if not body or len(body) > 1000:
        raise OperationsToolError("Touchpoint body must be 1-1000 characters.")

    resolved_category_name = category.name if category else category_name
    before = (
        {
            "id": str(reply.id),
            "category_id": str(reply.category_id),
            "category_name": reply.category.name,
            "title": reply.title,
            "body": reply.body,
            "active": reply.is_active,
        }
        if reply else None
    )
    after = {
        "category_id": str(category.id) if category else None,
        "category_name": resolved_category_name,
        "title": title,
        "body": body,
        "active": True,
    }
    return reply, category, before, after


def upsert_touchpoint(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="upsert_touchpoint",
        arguments=arguments,
    )
    reply, category, before, after = _touchpoint_proposal(
        organization=organization, arguments=arguments
    )
    proposal = {
        "organization_id": str(organization.id),
        "touchpoint_id": str(reply.id) if reply else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "operation": "update" if reply else "create",
                "after": after,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="touchpoint" if reply else "organization",
            target_id=str(reply.id) if reply else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_touchpoint",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    with transaction.atomic():
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        locked_reply, locked_category, locked_before, locked_after = _touchpoint_proposal(
            organization=organization, arguments=arguments
        )
        locked_proposal = {
            "organization_id": str(organization.id),
            "touchpoint_id": str(locked_reply.id) if locked_reply else None,
            "before": locked_before,
            "after": locked_after,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        if locked_category is None:
            try:
                locked_category = TouchpointCategory.objects.create(
                    organization=organization,
                    name=locked_after["category_name"],
                )
            except IntegrityError:
                locked_category = TouchpointCategory.objects.get(
                    organization=organization,
                    name=locked_after["category_name"],
                )
        if locked_reply is None:
            locked_reply = TouchpointReply(category=locked_category)
        locked_reply.category = locked_category
        locked_reply.title = locked_after["title"]
        locked_reply.body = locked_after["body"]
        locked_reply.is_active = True
        locked_reply.full_clean()
        locked_reply.save()
        locked_reply.refresh_from_db()
        if (
            locked_reply.title != locked_after["title"]
            or locked_reply.body != locked_after["body"]
            or not locked_reply.is_active
        ):
            raise OperationsToolError("Touchpoint verification failed.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "touchpoint": {
                "id": str(locked_reply.id),
                "category_id": str(locked_reply.category_id),
                "title": locked_reply.title,
                "body": locked_reply.body,
                "active": locked_reply.is_active,
            },
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="touchpoint",
        target_id=str(locked_reply.id),
        reason=reason,
        audit_summary={"operation": "upsert_touchpoint", "verification": "passed"},
    )


def archive_touchpoint(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="archive_touchpoint",
        arguments=arguments,
    )
    reply = (
        TouchpointReply.objects.select_related("category")
        .filter(
            pk=_uuid((arguments or {}).get("touchpoint_id"), field="touchpoint_id"),
            category__organization=organization,
        )
        .first()
    )
    if reply is None:
        raise OperationsToolError("Touchpoint not found in this organization.")
    proposal = {
        "touchpoint_id": str(reply.id),
        "before_active": reply.is_active,
        "after_active": False,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "touchpoint_id": str(reply.id),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="touchpoint",
            target_id=str(reply.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "archive_touchpoint",
                "proposal_digest": _proposal_digest(proposal),
            },
        )
    with transaction.atomic():
        locked = (
            TouchpointReply.objects.select_for_update()
            .filter(pk=reply.pk, category__organization=organization)
            .first()
        )
        if locked is None:
            raise OperationsApprovalRequired(
                "The Touchpoint changed after review. Run a fresh dry-run."
            )
        locked_proposal = {
            "touchpoint_id": str(locked.id),
            "before_active": locked.is_active,
            "after_active": False,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        locked.is_active = False
        locked.save(update_fields=["is_active", "updated_at"])
    return ToolExecution(
        data={"status": "FIXED", "touchpoint_id": str(reply.id), "active": False, "verification": "passed"},
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="touchpoint",
        target_id=str(reply.id),
        reason=reason,
        audit_summary={"operation": "archive_touchpoint", "verification": "passed"},
    )


def list_faqs(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    active_only = bool((arguments or {}).get("active_only", False))
    rows = [
        {
            "id": str(item.id),
            "question": item.question,
            "answer": item.answer,
            "active": item.is_active,
            "updated_at": item.updated_at.isoformat(),
        }
        for item in FAQService().list(
            organization=organization,
            active_only=active_only,
        )[:100]
    ]
    return ToolExecution(
        data={"faqs": rows, "count": len(rows)},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"faq_count": len(rows)},
    )


def _faq_id(value):
    try:
        faq_id = int(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise OperationsToolError("faq_id must be a positive integer.") from exc
    if faq_id <= 0:
        raise OperationsToolError("faq_id must be a positive integer.")
    return faq_id


def _faq_proposal(*, organization, arguments):
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be an FAQ object.")
    _reject_secret_like_content(data, field="faq")
    faq_id = str((arguments or {}).get("faq_id") or "").strip()
    faq = None
    if faq_id:
        faq = FAQ.objects.filter(
            pk=_faq_id(faq_id),
            organization=organization,
        ).first()
        if faq is None:
            raise OperationsToolError("FAQ not found in this organization.")
    question = str(data.get("question", faq.question if faq else "") or "").strip()
    answer = str(data.get("answer", faq.answer if faq else "") or "").strip()
    is_active = data.get("is_active", faq.is_active if faq else True)
    if not question or not answer:
        raise OperationsToolError("FAQ question and answer are required.")
    if not isinstance(is_active, bool):
        raise OperationsToolError("FAQ is_active must be a boolean.")
    before = (
        {"question": faq.question, "answer": faq.answer, "is_active": faq.is_active}
        if faq else None
    )
    after = {"question": question, "answer": answer, "is_active": is_active}
    return faq, before, after


def upsert_faq(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="upsert_faq",
        arguments=arguments,
    )
    faq, before, after = _faq_proposal(organization=organization, arguments=arguments)
    proposal = {
        "organization_id": str(organization.id),
        "faq_id": str(faq.id) if faq else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "operation": "update" if faq else "create",
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="faq" if faq else "organization",
            target_id=str(faq.id) if faq else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_faq",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    service = FAQService()
    with transaction.atomic():
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        locked_faq, locked_before, locked_after = _faq_proposal(
            organization=organization, arguments=arguments
        )
        locked_proposal = {
            "organization_id": str(organization.id),
            "faq_id": str(locked_faq.id) if locked_faq else None,
            "before": locked_before,
            "after": locked_after,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        try:
            if locked_faq:
                saved = service.update(
                    organization=organization,
                    faq_id=locked_faq.id,
                    data=locked_after,
                )
            else:
                saved = service.create(
                    organization=organization,
                    question=locked_after["question"],
                    answer=locked_after["answer"],
                    is_active=locked_after["is_active"],
                )
        except FAQServiceError as exc:
            raise OperationsToolError(str(exc)) from exc
    saved.refresh_from_db()
    return ToolExecution(
        data={
            "status": "FIXED",
            "faq": {
                "id": str(saved.id),
                "question": saved.question,
                "answer": saved.answer,
                "active": saved.is_active,
            },
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="faq",
        target_id=str(saved.id),
        reason=reason,
        audit_summary={"operation": "upsert_faq", "verification": "passed"},
    )


def archive_faq(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="archive_faq",
        arguments=arguments,
    )
    faq = FAQ.objects.filter(
        pk=_faq_id((arguments or {}).get("faq_id")),
        organization=organization,
    ).first()
    if faq is None:
        raise OperationsToolError("FAQ not found in this organization.")
    proposal = {"faq_id": str(faq.id), "before_active": faq.is_active, "after_active": False}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "faq_id": str(faq.id),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="faq",
            target_id=str(faq.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "archive_faq", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked = FAQ.objects.select_for_update().filter(
            pk=faq.pk, organization=organization
        ).first()
        if locked is None:
            raise OperationsApprovalRequired("The FAQ changed after review. Run a fresh dry-run.")
        locked_proposal = {"faq_id": str(locked.id), "before_active": locked.is_active, "after_active": False}
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        try:
            saved = FAQService().deactivate(organization=organization, faq_id=locked.id)
        except FAQServiceError as exc:
            raise OperationsToolError(str(exc)) from exc
    return ToolExecution(
        data={"status": "FIXED", "faq_id": str(saved.id), "active": saved.is_active, "verification": "passed"},
        capability=CAP_AI_CONFIG_WRITE,
        target_type="faq",
        target_id=str(saved.id),
        reason=reason,
        audit_summary={"operation": "archive_faq", "verification": "passed"},
    )


def _knowledge_source(organization, source_id):
    source = KnowledgeSource.objects.filter(
        pk=source_id,
        organization=organization,
    ).first()
    if source is None:
        raise OperationsToolError("Knowledge source not found in this organization.")
    return source


def create_knowledge_source(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="create_knowledge_source",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a knowledge source object.")
    _reject_secret_like_content(data, field="knowledge_source")
    source_type = str(data.get("source_type") or "url").strip()
    if source_type != KnowledgeSource.SourceType.URL:
        raise OperationsToolError(
            "create_knowledge_source supports URL sources; use upload_knowledge_document for files."
        )
    name = str(data.get("name") or "").strip()
    url = str(data.get("url") or "").strip()
    ingest = data.get("ingest", True)
    if not isinstance(ingest, bool):
        raise OperationsToolError("ingest must be a boolean.")
    try:
        normalized = KnowledgeIngestionService().normalize_url(url)
    except KnowledgeExtractionError as exc:
        raise OperationsToolError(str(exc)) from exc
    duplicate = KnowledgeSource.objects.filter(
        organization=organization,
        source_type=KnowledgeSource.SourceType.URL,
        url=normalized,
        is_active=True,
    ).first()
    if duplicate:
        raise OperationsToolError("An active knowledge URL source already exists.")
    proposal = {
        "organization_id": str(organization.id),
        "source_type": "url",
        "name": name,
        "url": normalized,
        "ingest": ingest,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "source_type": "url",
                "url_host": re.sub(r"^https?://", "", normalized).split("/", 1)[0],
                "ingest": ingest,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="organization",
            target_id=str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "create_knowledge_source", "proposal_digest": _proposal_digest(proposal)},
        )

    from apps.ai_engagement.tasks import ingest_and_index_url_source

    with transaction.atomic():
        locked_org = organization.__class__.objects.select_for_update().get(pk=organization.pk)
        if KnowledgeSource.objects.filter(
            organization=locked_org,
            source_type=KnowledgeSource.SourceType.URL,
            url=normalized,
            is_active=True,
        ).exists():
            raise OperationsApprovalRequired(
                "Knowledge sources changed after review. Run a fresh dry-run."
            )
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        try:
            source = KnowledgeSourceService().create_url_source(
                organization=locked_org,
                url=normalized,
                name=name,
            )
        except KnowledgeSourceServiceError as exc:
            raise OperationsToolError(str(exc)) from exc
        if ingest:
            transaction.on_commit(
                lambda source_id=source.id, org_id=locked_org.id: ingest_and_index_url_source.delay(
                    source_id, org_id
                )
            )
    return ToolExecution(
        data={
            "status": "FIXED",
            "source": {
                "id": str(source.id),
                "source_type": source.source_type,
                "name": source.name,
                "active": source.is_active,
            },
            "ingestion_queued": ingest,
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="knowledge_source",
        target_id=str(source.id),
        reason=reason,
        audit_summary={"operation": "create_knowledge_source", "ingestion_queued": ingest, "verification": "passed"},
    )


def _decode_upload(data):
    filename = str(data.get("filename") or "").strip()
    encoded = str(data.get("content_base64") or "").strip()
    if not filename or not encoded:
        raise OperationsToolError("filename and content_base64 are required.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OperationsToolError("content_base64 is not valid base64.") from exc
    if not raw or len(raw) > MAX_MCP_KNOWLEDGE_UPLOAD_BYTES:
        raise OperationsToolError("Knowledge upload through MCP must be between 1 byte and 512 KiB.")
    return filename, raw


def upload_knowledge_document(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="upload_knowledge_document",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a knowledge document object.")
    filename, raw = _decode_upload(data)
    name = str(data.get("name") or filename).strip()
    proposal = {
        "organization_id": str(organization.id),
        "filename": filename,
        "name": name,
        "size": len(raw),
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "filename": filename,
                "size": len(raw),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "ingestion_will_be_queued": True,
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="organization",
            target_id=str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "upload_knowledge_document", "proposal_digest": _proposal_digest(proposal), "size": len(raw)},
        )

    from apps.ai_engagement.tasks import ingest_and_index_document

    with transaction.atomic():
        locked_org = organization.__class__.objects.select_for_update().get(pk=organization.pk)
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        upload = ContentFile(raw, name=filename)
        try:
            source, document = KnowledgeSourceService().create_file_source(
                organization=locked_org,
                uploaded_file=upload,
                name=name,
            )
        except KnowledgeSourceServiceError as exc:
            raise OperationsToolError(str(exc)) from exc
        transaction.on_commit(
            lambda document_id=document.id, org_id=locked_org.id: ingest_and_index_document.delay(
                document_id, org_id
            )
        )
    return ToolExecution(
        data={
            "status": "FIXED",
            "source_id": str(source.id),
            "document": {
                "id": str(document.id),
                "name": document.name,
                "processing_status": document.processing_status,
                "active": document.is_active,
            },
            "ingestion_queued": True,
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="knowledge_document",
        target_id=str(document.id),
        reason=reason,
        audit_summary={"operation": "upload_knowledge_document", "source_id": str(source.id), "verification": "passed"},
    )


def publish_knowledge_document(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="publish_knowledge_document",
        arguments=arguments,
    )
    document = Document.objects.filter(
        pk=(arguments or {}).get("document_id"),
        organization=organization,
    ).annotate(
        active_chunk_count=Count(
            "chunks",
            filter=Q(chunks__is_active=True),
        ),
        embedded_chunk_count=Count(
            "chunks",
            filter=Q(chunks__embedding__isnull=False, chunks__is_active=True),
        ),
    ).first()
    if document is None:
        raise OperationsToolError("Knowledge document not found in this organization.")
    if document.processing_status != Document.ProcessingStatus.COMPLETED:
        raise OperationsToolError("Only completed knowledge documents can be published.")
    if not document.active_chunk_count or document.embedded_chunk_count != document.active_chunk_count:
        raise OperationsToolError("Knowledge document must be fully embedded before publication.")
    proposal = {
        "document_id": str(document.id),
        "source_key": document.source_key,
        "version": document.version,
        "before_active": document.is_active,
        "after_active": True,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "document_id": str(document.id),
                "version": document.version,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="knowledge_document",
            target_id=str(document.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "publish_knowledge_document", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(
            pk=document.pk, organization=organization
        ).first()
        if locked is None:
            raise OperationsApprovalRequired("Knowledge document changed after review. Run a fresh dry-run.")
        locked_proposal = {
            "document_id": str(locked.id),
            "source_key": locked.source_key,
            "version": locked.version,
            "before_active": locked.is_active,
            "after_active": True,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        try:
            saved = KnowledgeIngestionService().publish_document_version(locked)
        except KnowledgeExtractionError as exc:
            raise OperationsToolError(str(exc)) from exc
    return ToolExecution(
        data={"status": "FIXED", "document_id": str(saved.id), "active": saved.is_active, "verification": "passed"},
        capability=CAP_AI_CONFIG_WRITE,
        target_type="knowledge_document",
        target_id=str(saved.id),
        reason=reason,
        audit_summary={"operation": "publish_knowledge_document", "verification": "passed"},
    )


def archive_knowledge_document(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="archive_knowledge_document",
        arguments=arguments,
    )
    document = Document.objects.filter(
        pk=(arguments or {}).get("document_id"),
        organization=organization,
    ).first()
    if document is None:
        raise OperationsToolError("Knowledge document not found in this organization.")
    proposal = {
        "document_id": str(document.id),
        "before_active": document.is_active,
        "after_active": False,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "document_id": str(document.id),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "affected_chunk_count": document.chunks.filter(is_active=True).count(),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="knowledge_document",
            target_id=str(document.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "archive_knowledge_document", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(
            pk=document.pk, organization=organization
        ).first()
        if locked is None:
            raise OperationsApprovalRequired("Knowledge document changed after review. Run a fresh dry-run.")
        locked_proposal = {
            "document_id": str(locked.id),
            "before_active": locked.is_active,
            "after_active": False,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        # Retrieval already requires document.is_active=True. Keep chunk
        # activation intact so archive is genuinely reversible by republishing
        # this completed, embedded version later.
        locked.is_active = False
        locked.save(update_fields=["is_active", "updated_at"])
    return ToolExecution(
        data={"status": "FIXED", "document_id": str(document.id), "active": False, "verification": "passed"},
        capability=CAP_AI_CONFIG_WRITE,
        target_type="knowledge_document",
        target_id=str(document.id),
        reason=reason,
        audit_summary={"operation": "archive_knowledge_document", "verification": "passed"},
    )


def _cadence(organization, cadence_id, *, active=True):
    query = FollowupSequence.objects.filter(
        pk=_uuid(cadence_id, field="cadence_id"),
        organization=organization,
    )
    if active:
        query = query.filter(is_active=True)
    sequence = query.select_related("whatsapp_account").defer(
        "whatsapp_account__access_token"
    ).first()
    if sequence is None:
        raise OperationsToolError("Cadence not found in this organization.")
    return sequence


def _attachment_from_data(data):
    encoded = str(data.get("attachment_base64") or "").strip()
    if not encoded:
        return None
    filename = str(data.get("attachment_name") or "").strip()
    if not filename:
        raise OperationsToolError("attachment_name is required with attachment_base64.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OperationsToolError("attachment_base64 is invalid.") from exc
    if not raw or len(raw) > MAX_MCP_KNOWLEDGE_UPLOAD_BYTES:
        raise OperationsToolError("MCP attachment must be between 1 byte and 512 KiB.")
    upload = ContentFile(raw, name=filename)
    upload.content_type = str(data.get("attachment_mime_type") or "")
    return upload


def add_hosted_whatsapp_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="add_hosted_whatsapp_step",
        arguments=arguments,
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"))
    if sequence.whatsapp_account.connection_type != WhatsAppAccount.ConnectionType.coexisted:
        raise OperationsToolError("The selected Cadence is not a Hosted WhatsApp Cadence.")
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Hosted WhatsApp step object.")
    _reject_secret_like_content(
        {key: value for key, value in data.items() if key != "attachment_base64"},
        field="hosted_cadence_step",
    )
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    if not title or not body:
        raise OperationsToolError("Hosted WhatsApp step title and body are required.")
    schedule = _cadence_schedule(data)
    attachment_name = str(data.get("attachment_name") or "").strip()
    attachment_size = 0
    if data.get("attachment_base64"):
        try:
            attachment_size = len(base64.b64decode(str(data["attachment_base64"]), validate=True))
        except (binascii.Error, ValueError) as exc:
            raise OperationsToolError("attachment_base64 is invalid.") from exc
    proposal = {
        "cadence_id": str(sequence.id),
        "existing_step_count": sequence.steps.count(),
        "next_position": sequence.steps.count() + 1,
        "title": title,
        "body": body,
        "schedule": schedule,
        "attachment_name": attachment_name,
        "attachment_size": attachment_size,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "cadence_id": str(sequence.id),
                "next_position": proposal["next_position"],
                "schedule_type": schedule["schedule_type"],
                "attachment_size": attachment_size,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence",
            target_id=str(sequence.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "add_hosted_whatsapp_step", "proposal_digest": _proposal_digest(proposal)},
        )

    with transaction.atomic():
        sequence = (
            FollowupSequence.objects.select_for_update()
            .select_related("whatsapp_account")
            .filter(pk=sequence.pk, organization=organization, is_active=True)
            .first()
        )
        if sequence is None:
            raise OperationsApprovalRequired("The Cadence changed after review. Run a fresh dry-run.")
        locked_proposal = {
            **proposal,
            "existing_step_count": sequence.steps.count(),
            "next_position": sequence.steps.count() + 1,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        attachment = _attachment_from_data(data)
        try:
            step = domain_add_hosted_whatsapp_step(
                sequence=sequence,
                title=title,
                body=body,
                attachment=attachment,
                **schedule,
            )
        except (HostedAutomationError, FollowupError) as exc:
            raise OperationsToolError(str(exc)) from exc
        step.refresh_from_db()
        hosted = HostedFollowupStepConfig.objects.get(step=step)
        if hosted.body != body or step.position != locked_proposal["next_position"]:
            raise OperationsToolError("Hosted Cadence step verification failed.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "cadence_id": str(sequence.id),
            "step": {
                "id": str(step.id),
                "type": step.step_type,
                "position": step.position,
                "title": step.title,
                "body": hosted.body,
                "has_attachment": bool(hosted.attachment),
                "schedule_type": step.schedule_type,
            },
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence_step",
        target_id=str(step.id),
        reason=reason,
        audit_summary={"operation": "add_hosted_whatsapp_step", "cadence_id": str(sequence.id), "verification": "passed"},
    )


def _step_snapshot(step):
    hosted = None
    if (
        step.step_type == FollowupStep.StepType.WHATSAPP
        and step.sequence.whatsapp_account.connection_type == WhatsAppAccount.ConnectionType.coexisted
    ):
        hosted = HostedFollowupStepConfig.objects.filter(step=step).first()
    return {
        "id": str(step.id),
        "type": step.step_type,
        "position": step.position,
        "title": step.title,
        "template_id": str(step.whatsapp_template_id) if step.whatsapp_template_id else None,
        "email_subject": step.email_subject,
        "email_body": step.email_body,
        "reminder_text": step.reminder_text,
        "hosted_body": hosted.body if hosted else None,
        "hosted_attachment_name": hosted.attachment_original_name if hosted else "",
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


def update_cadence_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="update_cadence_step",
        arguments=arguments,
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"))
    step = sequence.steps.filter(
        pk=_uuid((arguments or {}).get("step_id"), field="step_id")
    ).select_related("sequence__whatsapp_account", "whatsapp_template").first()
    if step is None:
        raise OperationsToolError("Cadence step not found.")
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Cadence step object.")
    _reject_secret_like_content(
        {key: value for key, value in data.items() if key != "attachment_base64"},
        field="cadence_step",
    )
    schedule = _cadence_schedule(data)
    before = _step_snapshot(step)
    after = deepcopy(before)
    after["schedule"] = {
        "type": schedule["schedule_type"],
        "delay_value": schedule["delay_value"],
        "delay_unit": schedule["delay_unit"],
        "time": schedule["specific_time"].isoformat() if schedule["specific_time"] else None,
        "weekday": schedule["specific_weekday"],
        "recurring_every": schedule["recurring_every"],
        "recurring_unit": schedule["recurring_unit"],
        "weekdays": schedule["recurring_weekdays"],
    }
    after["is_active"] = data.get("is_active", step.is_active)
    if not isinstance(after["is_active"], bool):
        raise OperationsToolError("is_active must be a boolean.")

    hosted = (
        step.step_type == FollowupStep.StepType.WHATSAPP
        and sequence.whatsapp_account.connection_type == WhatsAppAccount.ConnectionType.coexisted
    )
    template = None
    if hosted:
        after["title"] = str(data.get("title", step.title) or "").strip()
        current_hosted = HostedFollowupStepConfig.objects.filter(step=step).first()
        after["hosted_body"] = str(
            data.get("body", current_hosted.body if current_hosted else "") or ""
        ).strip()
        if not after["title"] or not after["hosted_body"]:
            raise OperationsToolError("Hosted WhatsApp title and body are required.")
    elif step.step_type == FollowupStep.StepType.WHATSAPP:
        template_id = data.get("template_id", step.whatsapp_template_id)
        template = WhatsAppTemplate.objects.filter(
            pk=_uuid(template_id, field="template_id"),
            organization=organization,
            account=sequence.whatsapp_account,
            status=WhatsAppTemplate.Status.APPROVED,
        ).first()
        if template is None:
            raise OperationsToolError("Approved WhatsApp template not found for this Cadence account.")
        after["template_id"] = str(template.id)
        after["title"] = template.name
    elif step.step_type == FollowupStep.StepType.EMAIL:
        after["title"] = str(data.get("title", step.title) or "").strip()
        after["email_subject"] = str(data.get("subject", step.email_subject) or "").strip()
        after["email_body"] = str(data.get("body", step.email_body) or "").strip()
        if not after["email_subject"] or not after["email_body"]:
            raise OperationsToolError("Email Cadence step subject and body are required.")
    else:
        after["reminder_text"] = str(
            data.get("text", step.reminder_text) or ""
        ).strip()
        if not after["reminder_text"]:
            raise OperationsToolError("Reminder Cadence step text is required.")

    proposal = {"cadence_id": str(sequence.id), "step_id": str(step.id), "before": before, "after": after}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "cadence_id": str(sequence.id),
                "step_id": str(step.id),
                "after": after,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence_step",
            target_id=str(step.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "update_cadence_step", "proposal_digest": _proposal_digest(proposal)},
        )

    with transaction.atomic():
        locked_step = (
            FollowupStep.objects.select_for_update(of=("self",))
            .select_related("sequence__whatsapp_account", "whatsapp_template")
            .filter(
                pk=step.pk,
                sequence__organization=organization,
                sequence_id=sequence.id,
            )
            .first()
        )
        if locked_step is None:
            raise OperationsApprovalRequired("The Cadence step changed after review. Run a fresh dry-run.")
        locked_before = _step_snapshot(locked_step)
        locked_proposal = {
            "cadence_id": str(sequence.id),
            "step_id": str(locked_step.id),
            "before": locked_before,
            "after": after,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)

        if hosted:
            attachment = _attachment_from_data(data)
            try:
                domain_update_hosted_whatsapp_step(
                    step=locked_step,
                    title=after["title"],
                    body=after["hosted_body"],
                    attachment=attachment,
                    remove_attachment=bool(data.get("remove_attachment", False)),
                )
            except HostedAutomationError as exc:
                raise OperationsToolError(str(exc)) from exc
        elif locked_step.step_type == FollowupStep.StepType.WHATSAPP:
            locked_step.whatsapp_template = template
            locked_step.title = template.name
        elif locked_step.step_type == FollowupStep.StepType.EMAIL:
            locked_step.title = after["title"]
            locked_step.email_subject = after["email_subject"]
            locked_step.email_body = after["email_body"]
        else:
            locked_step.reminder_text = after["reminder_text"]

        for field, value in {
            "schedule_type": schedule["schedule_type"],
            "delay_value": schedule["delay_value"],
            "delay_unit": schedule["delay_unit"],
            "specific_time": schedule["specific_time"],
            "specific_weekday": schedule["specific_weekday"],
            "recurring_every": schedule["recurring_every"],
            "recurring_unit": schedule["recurring_unit"],
            "recurring_weekdays": list(schedule["recurring_weekdays"] or []),
            "is_active": after["is_active"],
        }.items():
            setattr(locked_step, field, value)
        try:
            _validate_schedule(
                schedule_type=locked_step.schedule_type,
                delay_value=locked_step.delay_value,
                delay_unit=locked_step.delay_unit,
                specific_time=locked_step.specific_time,
                specific_weekday=locked_step.specific_weekday,
                recurring_every=locked_step.recurring_every,
                recurring_unit=locked_step.recurring_unit,
                recurring_weekdays=locked_step.recurring_weekdays,
            )
        except FollowupError as exc:
            raise OperationsToolError(str(exc)) from exc
        locked_step.save()
        _recalculate_active_states(locked_step.sequence)
        locked_step.refresh_from_db()
        verification = _step_snapshot(locked_step)

    return ToolExecution(
        data={"status": "FIXED", "step": verification, "verification": "passed"},
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence_step",
        target_id=str(step.id),
        reason=reason,
        audit_summary={"operation": "update_cadence_step", "verification": "passed"},
    )


def delete_cadence_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="delete_cadence_step",
        arguments=arguments,
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"))
    step = sequence.steps.filter(
        pk=_uuid((arguments or {}).get("step_id"), field="step_id")
    ).first()
    if step is None:
        raise OperationsToolError("Cadence step not found.")
    execution_count = FollowupExecution.objects.filter(step=step).count()
    if execution_count:
        raise OperationsToolError(
            "Cadence step has delivery history and cannot be permanently deleted. Archive the Cadence or deactivate/edit the step instead."
        )
    before = _step_snapshot(
        FollowupStep.objects.select_related("sequence__whatsapp_account", "whatsapp_template").get(pk=step.pk)
    )
    proposal = {"cadence_id": str(sequence.id), "step_id": str(step.id), "before": before, "after": None}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "cadence_id": str(sequence.id),
                "step_id": str(step.id),
                "affected_execution_count": 0,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": False,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence_step",
            target_id=str(step.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "delete_cadence_step", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked = (
            FollowupStep.objects.select_for_update(of=("self",))
            .select_related("sequence__whatsapp_account", "whatsapp_template")
            .filter(pk=step.pk, sequence=sequence)
            .first()
        )
        if locked is None or FollowupExecution.objects.filter(step=locked).exists():
            raise OperationsApprovalRequired("Cadence step dependencies changed after review. Run a fresh dry-run.")
        locked_proposal = {
            "cadence_id": str(sequence.id),
            "step_id": str(locked.id),
            "before": _step_snapshot(locked),
            "after": None,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        delete_step(step=locked)
    return ToolExecution(
        data={"status": "FIXED", "step_id": str(step.id), "deleted": True, "verification": "passed"},
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence_step",
        target_id=str(step.id),
        reason=reason,
        audit_summary={"operation": "delete_cadence_step", "verification": "passed"},
    )


def reorder_cadence_steps(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="reorder_cadence_steps",
        arguments=arguments,
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"))
    step_ids = (arguments or {}).get("step_ids")
    if not isinstance(step_ids, list) or not all(isinstance(item, str) for item in step_ids):
        raise OperationsToolError("step_ids must be an ordered list of UUID strings.")
    steps = list(sequence.steps.order_by("position", "created_at"))
    existing_ids = [str(step.id) for step in steps]
    if len(step_ids) != len(steps) or set(step_ids) != set(existing_ids):
        raise OperationsToolError("step_ids must contain every current Cadence step exactly once.")
    proposal = {
        "cadence_id": str(sequence.id),
        "before": existing_ids,
        "after": step_ids,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "before": existing_ids,
                "after": step_ids,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence",
            target_id=str(sequence.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "reorder_cadence_steps", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked_steps = list(
            FollowupStep.objects.select_for_update()
            .filter(sequence=sequence)
            .order_by("position", "created_at")
        )
        locked_ids = [str(step.id) for step in locked_steps]
        locked_proposal = {"cadence_id": str(sequence.id), "before": locked_ids, "after": step_ids}
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        if len(step_ids) != len(locked_steps) or set(step_ids) != set(locked_ids):
            raise OperationsApprovalRequired("Cadence steps changed after review. Run a fresh dry-run.")
        by_id = {str(step.id): step for step in locked_steps}
        offset = len(locked_steps) + 1000
        for index, step in enumerate(locked_steps, start=1):
            step.position = offset + index
        FollowupStep.objects.bulk_update(locked_steps, ["position"])
        ordered = []
        for index, step_id in enumerate(step_ids, start=1):
            step = by_id[step_id]
            step.position = index
            ordered.append(step)
        FollowupStep.objects.bulk_update(ordered, ["position"])
        _recalculate_active_states(sequence)
    verified = list(
        sequence.steps.order_by("position", "created_at").values_list("id", flat=True)
    )
    if [str(item) for item in verified] != step_ids:
        raise OperationsToolError("Cadence reorder verification failed.")
    return ToolExecution(
        data={"status": "FIXED", "step_ids": step_ids, "verification": "passed"},
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence",
        target_id=str(sequence.id),
        reason=reason,
        audit_summary={"operation": "reorder_cadence_steps", "verification": "passed"},
    )


def simulate_cadence(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_DIAGNOSTICS_READ
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"), active=False)
    raw_reference = str((arguments or {}).get("reference_at") or "").strip()
    if raw_reference:
        try:
            reference = datetime.fromisoformat(raw_reference)
        except ValueError as exc:
            raise OperationsToolError("reference_at must be ISO-8601.") from exc
        if timezone.is_naive(reference):
            reference = timezone.make_aware(reference)
    else:
        reference = timezone.now()

    automation_settings = get_session_settings(account=sequence.whatsapp_account)
    rows = []
    cursor = reference
    for step in sequence.steps.filter(is_active=True).order_by("position", "created_at"):
        due = calculate_step_due(step=step, reference=cursor, organization=organization)
        due = _move_into_business_hours(
            organization=organization,
            due=due,
            automation_settings=automation_settings,
        )
        snapshot = _step_snapshot(
            FollowupStep.objects.select_related("sequence__whatsapp_account", "whatsapp_template").get(pk=step.pk)
        )
        rows.append(
            {
                "step_id": str(step.id),
                "position": step.position,
                "type": step.step_type,
                "title": step.title,
                "due_at": due.isoformat(),
                "content_preview": (
                    snapshot.get("hosted_body")
                    or snapshot.get("email_body")
                    or snapshot.get("reminder_text")
                    or ""
                )[:1000],
            }
        )
        cursor = due

    return ToolExecution(
        data={
            "simulation": True,
            "side_effects": False,
            "messages_sent": 0,
            "leads_created": 0,
            "workflow_actions_executed": 0,
            "cadence_id": str(sequence.id),
            "steps": rows,
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="cadence",
        target_id=str(sequence.id),
        audit_summary={"simulation": "cadence", "step_count": len(rows)},
    )


def simulate_workflow(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_DIAGNOSTICS_READ
    )
    lead = _lead(organization, (arguments or {}).get("lead_id"))
    workflow_id = str((arguments or {}).get("workflow_id") or "").strip()
    data = (arguments or {}).get("data")
    if workflow_id:
        from apps.triggers.models import SmartTrigger
        rule = SmartTrigger.objects.filter(
            pk=_uuid(workflow_id, field="workflow_id"),
            organization=organization,
        ).first()
        if rule is None:
            raise OperationsToolError("Workflow not found in this organization.")
        clean = {
            "name": rule.name,
            "enabled": rule.enabled,
            "trigger_type": rule.trigger_type,
            "conditions": rule.conditions,
            "action_type": rule.action_type,
            "action": rule.action,
            "fingerprint": rule.fingerprint,
        }
    else:
        _reject_secret_like_content(data, field="workflow")
        try:
            clean = validate_workflow_rule(organization, data)
        except ValidationError as exc:
            raise OperationsToolError("Workflow validation failed: " + "; ".join(exc.messages)) from exc
        _assert_workflow_safe_attribute_references(
            organization=organization,
            clean=clean,
        )
    _assert_workflow_safe_attribute_references(
        organization=organization,
        clean=clean,
    )
    rule_view = SimpleNamespace(
        conditions=clean["conditions"],
        trigger_type=clean["trigger_type"],
    )
    payload = (arguments or {}).get("event") or {}
    if not isinstance(payload, dict):
        raise OperationsToolError("event must be an object.")
    matched = bool(workflow_matches(rule_view, lead, payload))
    return ToolExecution(
        data={
            "simulation": True,
            "side_effects": False,
            "messages_sent": 0,
            "leads_created": 0,
            "workflow_actions_executed": 0,
            "matched": matched,
            "trigger_type": clean["trigger_type"],
            "planned_action": (
                {"action_type": clean["action_type"], "action": clean["action"]}
                if matched else None
            ),
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="lead",
        target_id=str(lead.id),
        audit_summary={"simulation": "workflow", "matched": matched, "trigger_type": clean["trigger_type"]},
    )


def _resolve_simulated_answer(requirements, key):
    raw = str(key or "").strip()
    q_match = re.fullmatch(r"q(?:uestion)?\s*(\d+)", raw, re.IGNORECASE)
    if q_match:
        priority = int(q_match.group(1))
        for item in requirements:
            if int(item.get("priority") or 0) == priority:
                return item
    for item in requirements:
        aliases = {
            str(item.get("id") or ""),
            str(item.get("stable_id") or ""),
            str(item.get("label") or ""),
        }
        if raw in aliases:
            return item
    return None


def simulate_ai_conversation(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_DIAGNOSTICS_READ
    )
    raw, sections, compiled, config = _safe_requirements_for_org(organization)
    requirements = list(compiled.get("requirements") or [])
    answers = (arguments or {}).get("answers") or {}
    if not isinstance(answers, dict):
        raise OperationsToolError("answers must be an object keyed by stable_id, requirement id, label, or Q number.")

    states = {}
    projected_attributes = {}
    from apps.ai_engagement.services.conditional_qualification_runtime import (
        ELIGIBILITY_ELIGIBLE,
        ELIGIBILITY_NOT_ELIGIBLE,
        requirement_eligibility,
    )
    from apps.ai_engagement.services.qualification_execution_contract import _mapped_value

    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "")
        eligibility = requirement_eligibility(requirement, states)
        states[requirement_id] = {
            "status": "not_applicable" if eligibility == ELIGIBILITY_NOT_ELIGIBLE else "unknown",
            "value": None,
        }

    for key, value in answers.items():
        requirement = _resolve_simulated_answer(requirements, key)
        if requirement is None:
            raise OperationsToolError(f"Unknown simulated qualification answer key: {key}.")
        requirement_id = str(requirement.get("id") or "")
        eligibility = requirement_eligibility(requirement, states)
        if eligibility != ELIGIBILITY_ELIGIBLE:
            raise OperationsToolError(
                f"Simulated answer {key} is not eligible under the supplied prior answers."
            )
        states[requirement_id] = {"status": "answered", "value": value}
        for target in (config.get("mapping_targets") or {}).get(requirement_id, []):
            projected_attributes[target] = _mapped_value(config, target, value)

        for later in requirements:
            later_id = str(later.get("id") or "")
            if states.get(later_id, {}).get("status") == "answered":
                continue
            later_eligibility = requirement_eligibility(later, states)
            states[later_id] = {
                "status": "not_applicable" if later_eligibility == ELIGIBILITY_NOT_ELIGIBLE else "unknown",
                "value": None,
            }

    next_requirement = None
    missing_required = []
    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "")
        eligibility = requirement_eligibility(requirement, states)
        status = str(states.get(requirement_id, {}).get("status") or "unknown")
        if eligibility != ELIGIBILITY_ELIGIBLE:
            continue
        if requirement.get("required", True) and status != "answered":
            missing_required.append(requirement_id)
        if next_requirement is None and status != "answered" and (
            requirement.get("required", True) or requirement.get("ask_when_eligible", True)
        ):
            next_requirement = requirement

    criteria = evaluate_playbook_criteria(
        raw,
        requirements=requirements,
        state={"requirement_states": states},
        values=projected_attributes,
    )
    completed = not missing_required
    would_move = bool(completed and criteria.get("qualified") and config.get("completion_stage"))
    return ToolExecution(
        data={
            "simulation": True,
            "side_effects": False,
            "messages_sent": 0,
            "leads_created": 0,
            "workflow_actions_executed": 0,
            "qualification": {
                "flow_version": compiled.get("flow_version"),
                "completed": completed,
                "criteria": criteria,
                "missing_requirement_ids": missing_required,
                "next_requirement": (
                    {
                        "id": next_requirement.get("id"),
                        "stable_id": next_requirement.get("stable_id"),
                        "question": next_requirement.get("question"),
                        "options": next_requirement.get("options") or [],
                    }
                    if next_requirement else None
                ),
                "projected_attributes": projected_attributes,
                "would_move_to_completion_stage": would_move,
                "completion_stage": (
                    {
                        "id": str(config["completion_stage"]["id"]),
                        "name": config["completion_stage"]["name"],
                        "pipeline_id": str(config["completion_stage"]["pipeline_id"]),
                    }
                    if config.get("completion_stage") else None
                ),
                "final_ack": config.get("final_ack") if would_move else "",
            },
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "simulation": "ai_conversation",
            "answer_count": len(answers),
            "completed": completed,
            "would_move_to_completion_stage": would_move,
        },
    )


EXTENDED_HANDLERS = {
    "get_qualification_configuration": get_qualification_configuration,
    "validate_qualification_configuration": validate_qualification_configuration,
    "upsert_qualification_configuration": upsert_qualification_configuration,
    "list_whatsapp_accounts": list_whatsapp_accounts,
    "begin_whatsapp_connection": begin_whatsapp_connection,
    "bind_whatsapp_account_to_pipeline": bind_whatsapp_account_to_pipeline,
    "validate_whatsapp_routing": validate_whatsapp_routing,
    "list_workflow_triggers": list_workflow_triggers,
    "list_workflow_actions": list_workflow_actions,
    "get_workflow_schema": get_workflow_schema,
    "validate_workflow_configuration": validate_workflow_configuration,
    "list_touchpoints": list_touchpoints,
    "upsert_touchpoint": upsert_touchpoint,
    "archive_touchpoint": archive_touchpoint,
    "list_faqs": list_faqs,
    "upsert_faq": upsert_faq,
    "archive_faq": archive_faq,
    "create_knowledge_source": create_knowledge_source,
    "upload_knowledge_document": upload_knowledge_document,
    "publish_knowledge_document": publish_knowledge_document,
    "archive_knowledge_document": archive_knowledge_document,
    "add_hosted_whatsapp_step": add_hosted_whatsapp_step,
    "update_cadence_step": update_cadence_step,
    "delete_cadence_step": delete_cadence_step,
    "reorder_cadence_steps": reorder_cadence_steps,
    "simulate_ai_conversation": simulate_ai_conversation,
    "simulate_workflow": simulate_workflow,
    "simulate_cadence": simulate_cadence,
}
