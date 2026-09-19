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


_ATTRIBUTE_STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "to", "current", "lead", "customer",
    "number", "count", "size",
}
_ATTRIBUTE_ALIASES = {
    "employees": "team", "employee": "team", "people": "team", "staff": "team",
    "salespeople": "sales", "salesperson": "sales", "reps": "sales", "representatives": "sales",
    "crm": "crm", "software": "tool", "system": "tool", "platform": "tool",
}
_SENSITIVE_ATTRIBUTE_TERMS = {
    "password", "passcode", "otp", "token", "secret", "api key", "apikey",
    "credit card", "card number", "cvv", "bank account", "aadhaar", "aadhar",
    "pan number", "passport", "private key",
}


def _attribute_tokens(value: Any) -> set[str]:
    tokens = set()
    for raw in re.findall(r"[a-z0-9]+", _normalize_text(value)):
        token = _ATTRIBUTE_ALIASES.get(raw, raw)
        if token and token not in _ATTRIBUTE_STOPWORDS:
            tokens.add(token)
    return tokens


def _attribute_definitions(context) -> list[dict[str, Any]]:
    from apps.ai_engagement.services.confidentiality import (
        is_sensitive_attribute_definition,
    )

    definitions = (context.pipeline or {}).get("attribute_definitions") or []
    return [
        item
        for item in definitions
        if (
            isinstance(item, dict)
            and str(item.get("key") or "").strip()
            and not is_sensitive_attribute_definition(item)
        )
    ]


def _option_matches_numeric(option: Any, latest_text: str) -> bool:
    actual = _numeric(latest_text)
    if actual is None:
        return False
    normalized = _normalize_text(option).replace(",", "")
    plus = re.search(r"(\d+(?:\.\d+)?)\s*\+$", normalized)
    if plus:
        return actual >= float(plus.group(1))
    interval = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)", normalized)
    if interval:
        low, high = float(interval.group(1)), float(interval.group(2))
        return low <= actual <= high
    below = re.search(r"\b(?:below|under|less than)\s*(\d+(?:\.\d+)?)\b", normalized)
    if below:
        return actual < float(below.group(1))
    upto = re.search(r"\b(?:up to|upto)\s*(\d+(?:\.\d+)?)\b", normalized)
    if upto:
        return actual <= float(upto.group(1))
    return False


def _value_supported_by_definition(value: Any, latest_text: str, definition: dict[str, Any]) -> bool:
    if _value_supported_by_latest_message(value, latest_text):
        return True
    if str(definition.get("field_type") or "").casefold() != "option":
        return False
    rendered = _normalize_text(value)
    for option in definition.get("options") or []:
        if _normalize_text(option) == rendered and _option_matches_numeric(option, latest_text):
            return True
    return False


def _resolve_existing_attribute_key(candidate: str, definitions: list[dict[str, Any]]) -> str | None:
    normalized = _normalize_text(candidate).replace("_", " ")
    if normalized.startswith("new:"):
        normalized = normalized[4:].strip()
    if not normalized:
        return None

    for item in definitions:
        key = str(item.get("key") or "").strip()
        name = _normalize_text(item.get("name"))
        if candidate == key or normalized == key.replace("_", " ") or normalized == name:
            return key

    wanted = _attribute_tokens(normalized)
    if not wanted:
        return None
    ranked: list[tuple[float, str]] = []
    for item in definitions:
        key = str(item.get("key") or "").strip()
        existing = _attribute_tokens(f"{item.get('name') or ''} {key.replace('_', ' ')}")
        if not existing:
            continue
        union = wanted | existing
        score = len(wanted & existing) / len(union) if union else 0.0
        if wanted.issubset(existing) or existing.issubset(wanted):
            score = max(score, 0.85)
        if score >= 0.72:
            ranked.append((score, key))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return None
    if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < 0.08:
        return None
    return ranked[0][1]


def _safe_candidate_name(raw_key: str, proposed_name: str = "") -> str | None:
    name = ""
    if raw_key.casefold().startswith("new:"):
        name = raw_key.split(":", 1)[1]
    elif proposed_name:
        name = proposed_name
    name = re.sub(r"\s+", " ", name).strip(" .:_-")
    if not (3 <= len(name) <= 60):
        return None
    if len(name.split()) > 6:
        return None
    lowered = name.casefold()
    if any(term in lowered for term in _SENSITIVE_ATTRIBUTE_TERMS):
        return None
    if not re.search(r"[a-zA-Z]", name):
        return None

    from apps.ai_engagement.services.confidentiality import (
        is_sensitive_attribute_definition,
    )
    if is_sensitive_attribute_definition({"key": raw_key, "name": name}):
        return None
    return name


def _attribute_key_from_name(name: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9]+", "_", str(name or "").strip().lower())
    return re.sub(r"_+", "_", key).strip("_")[:100]


def _dynamic_attribute_update(
    *,
    update: dict[str, Any],
    value: Any,
    latest_text: str,
) -> dict[str, Any] | None:
    raw_key = str(update.get("key") or "").strip()
    proposed_name = str(update.get("name") or "").strip()
    explicitly_dynamic = (
        raw_key.casefold().startswith("new:")
        or update.get("create_if_missing") is True
    )
    if not explicitly_dynamic or not _value_supported_by_latest_message(value, latest_text):
        return None

    name = _safe_candidate_name(raw_key, proposed_name)
    if name is None:
        return None

    requested_type = str(update.get("field_type") or "").strip().casefold()
    if requested_type in {"text", "numeric", "date", "datetime"}:
        field_type = requested_type
    else:
        numeric = _numeric(value)
        field_type = (
            "numeric"
            if numeric is not None
            and re.fullmatch(
                r"[₹$]?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|thousand|lakh|lac|m|million|cr|crore)?",
                str(value or "").strip(),
                flags=re.IGNORECASE,
            )
            else "text"
        )

    if raw_key.casefold().startswith("new:"):
        key = _attribute_key_from_name(name)
    else:
        key = raw_key.casefold()
        if not re.fullmatch(r"[a-z0-9_]+", key):
            key = _attribute_key_from_name(name)
    if not key:
        return None

    return {
        "key": key,
        "value": value,
        "name": name,
        "field_type": field_type,
        "create_if_missing": True,
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
    attribute_definitions = _attribute_definitions(context)
    attribute_by_key = {
        str(item.get("key") or "").strip(): item
        for item in attribute_definitions
        if str(item.get("key") or "").strip()
    }

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
                if not isinstance(update, dict):
                    continue
                raw_key = str(update.get("key") or "").strip()
                proposed_name = str(update.get("name") or "").strip()
                value = update.get("value")
                if not _value_supported_by_latest_message(value, latest_text):
                    continue

                key = _resolve_existing_attribute_key(
                    raw_key if not proposed_name else f"{raw_key} {proposed_name}",
                    attribute_definitions,
                )
                definition = attribute_by_key.get(key or "")
                if key and definition:
                    if _value_supported_by_definition(value, latest_text, definition):
                        accepted.append({"key": key, "value": value})
                    continue

                if len(attribute_definitions) >= 15:
                    continue
                dynamic_update = _dynamic_attribute_update(
                    update=update,
                    value=value,
                    latest_text=latest_text,
                )
                if dynamic_update is not None:
                    accepted.append(dynamic_update)
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
