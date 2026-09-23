"""Qualification configuration tools for SHVYA Operations MCP."""

from __future__ import annotations

import re
from copy import deepcopy

from django.db import transaction

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.playbook import (
    SECTION_TITLES,
    parse_playbook,
    validate_playbook,
)
from apps.crm.models import AttributeDefinition, Stage
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    approval_required,
)
from apps.integrations.operations_tools import (
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _require_operations_capability,
    _sensitive_attribute_keys,
    _uuid,
    _write_gate,
)

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
