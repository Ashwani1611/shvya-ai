from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from apps.ai_engagement.services.qualification_state import project_answer_updates


_TEMPORAL_TERMS = re.compile(
    r"\b(?:remind|reminder|call|contact|follow\s*up|tomorrow|today|tonight|"
    r"next\s+(?:week|month)|this\s+(?:week|month)|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|\d{1,2}[:.]\d{2}|am|pm)\b",
    flags=re.IGNORECASE,
)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _normalize_text(value).replace(",", "").replace("₹", "").replace("$", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if re.search(r"\b(?:k|thousand)\b", text) or text.rstrip().endswith("k"):
        number *= 1_000
    elif re.search(r"\b(?:lakh|lac)\b", text) or text.rstrip().endswith("l"):
        number *= 100_000
    elif re.search(r"\b(?:crore|cr)\b", text):
        number *= 10_000_000
    return number


def _truthy(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _normalize_text(value)
    if text in {"yes", "y", "yeah", "yep", "true", "interested", "available"}:
        return True
    if text in {"no", "n", "nope", "false", "not interested", "unavailable"}:
        return False
    return None


def evaluate_condition(value: Any, condition: dict[str, Any] | None) -> str:
    if not condition:
        return "pass"
    operator = str(condition.get("operator") or "").strip().casefold()
    target = condition.get("value")

    if operator in {"gt", "gte", "lt", "lte"}:
        actual_num = _numeric(value)
        target_num = _numeric(target)
        if actual_num is None or target_num is None:
            return "unknown"
        checks = {
            "gt": actual_num > target_num,
            "gte": actual_num >= target_num,
            "lt": actual_num < target_num,
            "lte": actual_num <= target_num,
        }
        return "pass" if checks[operator] else "fail"

    if operator in {"truthy", "falsy"}:
        actual_bool = _truthy(value)
        if actual_bool is None:
            return "unknown"
        expected = operator == "truthy"
        return "pass" if actual_bool is expected else "fail"

    if operator == "eq":
        return "pass" if _normalize_text(value) == _normalize_text(target) else "fail"

    return "unknown"


def evaluate_qualification(*, runtime_policy: dict[str, Any], projected_state: dict[str, Any]) -> dict[str, Any]:
    criteria = (runtime_policy.get("qualification") or {}).get("criteria") or []
    requirement_states = projected_state.get("requirement_states") or {}
    results: list[dict[str, Any]] = []
    required_missing = False
    required_failed = False
    required_unknown = False

    for criterion in criteria:
        criterion_id = str(criterion.get("id") or "")
        state = requirement_states.get(criterion_id) or {}
        status = str(state.get("status") or "unknown")
        required = bool(criterion.get("required", True))
        if status not in {"answered", "not_applicable"}:
            verdict = "unknown"
            if required:
                required_missing = True
        elif status == "not_applicable":
            verdict = "pass" if not required else "unknown"
            if required:
                required_unknown = True
        else:
            verdict = evaluate_condition(state.get("value"), criterion.get("pass_condition"))
            if required and verdict == "fail":
                required_failed = True
            elif required and verdict == "unknown":
                required_unknown = True
        results.append(
            {
                "criterion_id": criterion_id,
                "required": required,
                "status": status,
                "verdict": verdict,
                "value": state.get("value"),
            }
        )

    if required_failed:
        outcome = "not_qualified"
    elif required_missing or required_unknown:
        outcome = "in_progress"
    elif criteria:
        outcome = "qualified"
    else:
        outcome = "not_configured"

    return {"outcome": outcome, "criteria": results}


def _value_supported_by_latest_message(value: Any, latest_text: str) -> bool:
    if value is None:
        return False
    latest = _normalize_text(latest_text)
    if not latest:
        return False
    if isinstance(value, bool):
        return _truthy(latest) is value
    rendered = _normalize_text(value)
    if rendered and rendered in latest:
        return True
    actual_num = _numeric(value)
    latest_num = _numeric(latest_text)
    return actual_num is not None and latest_num is not None and actual_num == latest_num


def _attribute_keys(context) -> set[str]:
    definitions = (context.pipeline or {}).get("attribute_definitions") or []
    return {
        str(item.get("key") or "").strip()
        for item in definitions
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }


def _qualification_attribute_updates(*, runtime_policy, qualification_updates, context) -> list[dict[str, Any]]:
    keys = _attribute_keys(context)
    if not keys:
        return []
    criteria = {
        str(item.get("id") or ""): item
        for item in (runtime_policy.get("qualification") or {}).get("criteria") or []
        if isinstance(item, dict)
    }
    updates: list[dict[str, Any]] = []
    for answer in qualification_updates or []:
        criterion = criteria.get(str(answer.get("requirement_id") or "")) or {}
        candidates = [
            str(criterion.get("attribute_key") or "").strip(),
            str(criterion.get("id") or "").strip(),
        ]
        key = next((candidate for candidate in candidates if candidate and candidate in keys), None)
        if key:
            updates.append({"key": key, "value": answer.get("value")})
    return updates


def build_controlled_actions(
    *,
    decision,
    context,
    runtime_policy: dict[str, Any],
    qualification_state: dict[str, Any],
    requirements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convert model proposals into evidence-bound CRM actions.

    The language model may propose useful extraction actions, but it never owns
    qualification stage movement. Pipeline/stage progression is planned here
    from validated qualification evidence and application state.
    """

    messages = (context.conversation or {}).get("messages", [])
    projected = project_answer_updates(
        state=qualification_state,
        requirements=requirements,
        updates=getattr(decision, "qualification_updates", []) or [],
        messages=messages,
    )
    evaluation = evaluate_qualification(
        runtime_policy=runtime_policy,
        projected_state=projected,
    )

    latest_text = ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("direction") == "inbound":
            latest_text = str(message.get("body") or "")
            if latest_text.strip():
                break

    controlled: list[dict[str, Any]] = []
    attribute_keys = _attribute_keys(context)
    qualification_values = {
        _normalize_text(item.get("value"))
        for item in getattr(decision, "qualification_updates", []) or []
    }

    for action in getattr(decision, "crm_actions", []) or []:
        action_type = action.get("type")
        if action_type == "pipeline_transition":
            # Never accept a model-selected stage. A deterministic transition is
            # added below only after qualification evaluation.
            continue
        if action_type == "add_note":
            # Qualification notes are generated by the dedicated summary path.
            # Do not let a chat-generation model create arbitrary CRM notes.
            continue
        if action_type == "attribute_updates":
            accepted = []
            for update in action.get("updates") or []:
                key = str(update.get("key") or "").strip()
                value = update.get("value")
                if key not in attribute_keys:
                    continue
                if _normalize_text(value) in qualification_values or _value_supported_by_latest_message(value, latest_text):
                    accepted.append({"key": key, "value": value})
            if accepted:
                controlled.append({"type": "attribute_updates", "updates": accepted})
            continue
        if action_type == "create_reminder":
            if _TEMPORAL_TERMS.search(latest_text):
                controlled.append(deepcopy(action))
            continue
        if action_type == "contact_updates":
            accepted = []
            for update in action.get("updates") or []:
                handle = str(update.get("handle") or "").strip()
                if handle and handle in latest_text:
                    accepted.append(deepcopy(update))
            if accepted:
                controlled.append({"type": "contact_updates", "updates": accepted})
            continue

    deterministic_attrs = _qualification_attribute_updates(
        runtime_policy=runtime_policy,
        qualification_updates=getattr(decision, "qualification_updates", []) or [],
        context=context,
    )
    if deterministic_attrs:
        existing = next((item for item in controlled if item.get("type") == "attribute_updates"), None)
        if existing is None:
            controlled.append({"type": "attribute_updates", "updates": deterministic_attrs})
        else:
            by_key = {item["key"]: item for item in existing["updates"]}
            for item in deterministic_attrs:
                by_key[item["key"]] = item
            existing["updates"] = list(by_key.values())

    if evaluation["outcome"] == "qualified":
        qualified_stage_id = qualification_state.get("qualified_stage_id")
        if qualified_stage_id:
            controlled.append(
                {
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": str(qualified_stage_id)},
                }
            )

    return controlled, {"projected_state": projected, "evaluation": evaluation}
