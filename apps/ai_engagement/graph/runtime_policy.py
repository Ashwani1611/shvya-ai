from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from django.core.cache import cache


POLICY_VERSION = 2
POLICY_CACHE_SECONDS = 3600
MAX_ENGAGEMENT_RULES = 30
MAX_RULE_CHARS = 320


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _split_rules(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[\n;]+", text)
    rules: list[str] = []
    for part in parts:
        rule = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", part).strip()
        rule = _compact(rule)
        if not rule:
            continue
        rules.append(rule[:MAX_RULE_CHARS])
        if len(rules) >= MAX_ENGAGEMENT_RULES:
            break
    return rules


def _number(value: str) -> float | None:
    normalized = value.casefold().replace(",", "").replace("₹", "").replace("$", "")
    multiplier = 1.0
    if normalized.endswith("k"):
        normalized = normalized[:-1]
        multiplier = 1_000.0
    elif normalized.endswith("l") or normalized.endswith("lac") or normalized.endswith("lakh"):
        normalized = re.sub(r"(?:l|lac|lakh)$", "", normalized)
        multiplier = 100_000.0
    try:
        return float(normalized.strip()) * multiplier
    except (TypeError, ValueError):
        return None


def _condition_for_requirement(requirement: dict[str, Any]) -> dict[str, Any] | None:
    """Compile common authored threshold language without another model call.

    The compiler is deliberately conservative. If it cannot prove a condition
    from the authored text it returns None and the criterion remains an
    information-gathering requirement rather than inventing a pass/fail rule.
    """

    text = _compact(
        " ".join(
            str(requirement.get(key) or "")
            for key in ("label", "question")
        )
    )
    lowered = text.casefold()
    number_match = re.search(
        r"(?:₹|\$)?\s*(\d[\d,]*(?:\.\d+)?\s*(?:k|l|lac|lakh)?)",
        lowered,
    )
    numeric_value = _number(number_match.group(1)) if number_match else None

    if numeric_value is not None:
        if re.search(r"\b(?:at least|minimum|min\.?|not less than)\b", lowered):
            return {"operator": "gte", "value": numeric_value}
        if re.search(r"\b(?:more than|greater than|above|over)\b", lowered):
            return {"operator": "gt", "value": numeric_value}
        if re.search(r"\b(?:at most|maximum|max\.?|not more than)\b", lowered):
            return {"operator": "lte", "value": numeric_value}
        if re.search(r"\b(?:less than|below|under)\b", lowered):
            return {"operator": "lt", "value": numeric_value}

    if re.search(r"\b(?:must|should|needs? to)\s+(?:be\s+)?(?:yes|interested|available)\b", lowered):
        return {"operator": "truthy", "value": True}
    if re.search(r"\b(?:must|should|needs? to)\s+(?:be\s+)?(?:no|not interested|unavailable)\b", lowered):
        return {"operator": "falsy", "value": False}
    return None


def compile_runtime_policy(*, organization, profile: dict[str, Any]) -> dict[str, Any]:
    qualification = profile.get("qualification") if isinstance(profile, dict) else {}
    qualification = qualification if isinstance(qualification, dict) else {}
    communication = profile.get("communication") if isinstance(profile, dict) else {}
    communication = communication if isinstance(communication, dict) else {}

    criteria: list[dict[str, Any]] = []
    for raw in qualification.get("requirements") or []:
        if not isinstance(raw, dict):
            continue
        item = {
            "id": str(raw.get("id") or "").strip(),
            "label": _compact(raw.get("label")),
            "question": _compact(raw.get("question")),
            "required": bool(raw.get("required", True)),
            "priority": int(raw.get("priority") or 999999),
            "can_direct_ask": bool(raw.get("can_direct_ask")),
        }
        if not item["id"]:
            continue
        condition = _condition_for_requirement(raw)
        if condition is not None:
            item["pass_condition"] = condition
        criteria.append(item)

    source = {
        "version": POLICY_VERSION,
        "organization_id": str(getattr(organization, "id", "")),
        "about": _compact((profile.get("identity") or {}).get("about")),
        "languages": communication.get("languages") or [],
        "engagement_instructions": _compact(communication.get("custom_instructions")),
        "qualification_raw": _compact(qualification.get("raw")),
        "criteria": criteria,
    }
    source_hash = hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return {
        "version": POLICY_VERSION,
        "source_hash": source_hash,
        "organization": {
            "name": _compact((profile.get("identity") or {}).get("name")),
            "about": source["about"],
            "languages": list(source["languages"]),
        },
        "engagement": {
            "rules": _split_rules(source["engagement_instructions"]),
            "answer_lead_question_first": True,
            "max_qualification_questions_per_turn": 1,
            "never_invent_org_facts": True,
            "never_expose_internal_reasoning": True,
            "respect_opt_out": True,
        },
        "qualification": {
            "mode": str(qualification.get("mode") or "configured"),
            "criteria": criteria,
        },
        "knowledge": {
            "retrieve_only_when_needed": True,
            "unknown_fact_behavior": "human_confirmation",
        },
    }


def get_runtime_policy(*, organization, profile: dict[str, Any]) -> dict[str, Any]:
    """Return the compact compiled policy from a source-hash keyed cache.

    OrgInfo remains the only editable source of truth. Runtime engagement never
    rewrites Organization.settings, avoiding stale JSON writes from workers.
    Recompilation is deterministic and local, so a cache miss costs no AI call.
    """

    compiled = compile_runtime_policy(organization=organization, profile=profile)
    cache_key = (
        f"shvya:ai:runtime-policy:{getattr(organization, 'id', 'unknown')}:"
        f"v{POLICY_VERSION}:{compiled['source_hash']}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return deepcopy(cached)
    cache.set(cache_key, compiled, POLICY_CACHE_SECONDS)
    return deepcopy(compiled)
