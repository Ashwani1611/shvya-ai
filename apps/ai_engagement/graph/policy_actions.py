from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION,
    project_answer_updates,
)


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
        if status not in {"answered", "not_applicable", "skipped"}:
            verdict = "unknown"
            if required:
                required_missing = True
        elif status in {"not_applicable", "skipped"}:
            verdict = "pass"
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
    from apps.ai_engagement.services.confidentiality import (
        is_sensitive_attribute_definition,
    )

    definitions = (context.pipeline or {}).get("attribute_definitions") or []
    return {
        str(item.get("key") or "").strip()
        for item in definitions
        if (
            isinstance(item, dict)
            and str(item.get("key") or "").strip()
            and not is_sensitive_attribute_definition(item)
        )
    }


def build_controlled_actions(
    *,
    decision,
    context,
    runtime_policy: dict[str, Any],
    qualification_state: dict[str, Any],
    requirements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter model proposals without owning qualification CRM execution.

    This core policy layer may project answers for deterministic qualification
    evaluation, but it does NOT map qualification answers to CRM attributes and
    does NOT choose a completion stage. Exact qualification mappings and the
    configured completion action are built by qualification_execution_contract
    before mutation. Normal-conversation CRM proposals remain evidence-bound.
    """

    messages = (context.conversation or {}).get("messages", [])
    qualification_active = (
        str(qualification_state.get("engagement_mode") or "").strip().casefold()
        == MODE_QUALIFICATION
    )
    qualification_updates = (
        getattr(decision, "qualification_updates", []) or []
        if qualification_active
        else []
    )
    projected = project_answer_updates(
        state=qualification_state,
        requirements=requirements,
        updates=qualification_updates,
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

    for action in getattr(decision, "crm_actions", []) or []:
        action_type = action.get("type")
        if action_type == "pipeline_transition":
            # A model never chooses a stage. Ordinary evidence-bound routing is
            # reintroduced by the dedicated routing layer; deterministic
            # qualification completion is built by the execution contract.
            continue
        if action_type == "add_note":
            # Internal summaries/notes are owned by dedicated backend services.
            continue
        if action_type == "attribute_updates":
            # Exact qualification requirement -> attribute mappings remain
            # backend-owned, but a lead may volunteer other CRM facts on the same
            # turn (company, website, industry, budget, etc.). Keep those
            # organization-defined updates only when the proposed value is
            # directly supported by the latest inbound customer message. The
            # deterministic qualification execution contract subsequently
            # overwrites any explicitly mapped key with its authoritative value.
            accepted = []
            for update in action.get("updates") or []:
                key = str(update.get("key") or "").strip()
                value = update.get("value")
                if key not in attribute_keys:
                    continue
                if _value_supported_by_latest_message(value, latest_text):
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

    return controlled, {"projected_state": projected, "evaluation": evaluation}
