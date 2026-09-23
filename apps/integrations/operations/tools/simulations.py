"""Read-only Operations MCP simulations and diagnostics."""

from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from apps.ai_engagement.services.playbook import evaluate_playbook_criteria
from apps.followups.models import FollowupStep
from apps.integrations.operations_policy import CAP_DIAGNOSTICS_READ
from services.channels.hosted_whatsapp_service import get_session_settings
from services.followup_service import _move_into_business_hours, calculate_step_due
from services.triggers.evaluator import matches as workflow_matches
from services.triggers.rules import validate as validate_workflow_rule
from apps.integrations.operations_tools import (
    OperationsToolError,
    ToolExecution,
    _assert_workflow_safe_attribute_references,
    _lead,
    _organization_for,
    _reject_secret_like_content,
    _require_operations_capability,
    _uuid,
)
from .cadence import _cadence, _step_snapshot
from .qualification import _safe_requirements_for_org

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
