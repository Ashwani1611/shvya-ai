from __future__ import annotations

import re
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from django.core.cache import cache
from django.utils.text import slugify


PROFILE_VERSION = 1
PROFILE_CACHE_SECONDS = 300


def _clean_requirement_line(value: str) -> str:
    text = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)


def _split_compact_list(text: str) -> list[str] | None:
    """Split simple authored lists such as 'Identify budget, timeline and city'."""
    normalized = text.strip()
    match = re.match(
        r"^(?:identify|collect|capture|ask\s+for|qualify\s+(?:on|using|based\s+on))\s+(.+)$",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    body = match.group(1).strip().rstrip(".")
    body = re.sub(r",?\s+and\s+", ",", body, flags=re.IGNORECASE)
    values = [_clean_requirement_line(value) for value in body.split(",")]
    values = [value for value in values if value]
    if 2 <= len(values) <= 10:
        return values
    return None


def _split_requirement_text(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []

    parts = re.split(r"[\n;]+", text)
    if len(parts) == 1 and text.count("?") > 1:
        parts = re.split(r"(?<=\?)\s+", text)
    elif len(parts) == 1 and "?" not in text:
        compact = _split_compact_list(text)
        if compact:
            parts = compact

    cleaned: list[str] = []
    for part in parts:
        item = _clean_requirement_line(part)
        if item:
            cleaned.append(item)
    return cleaned


def _qualification_mode(raw: str) -> str:
    text = str(raw or "").casefold()
    if any(
        phrase in text
        for phrase in (
            "all questions required",
            "all questions are required",
            "all requirements required",
            "all requirements are required",
            "every question must",
            "every requirement must",
        )
    ):
        return "all_required"
    if "majority" in text:
        return "majority"
    return "configured"


def _is_policy_line(value: str) -> bool:
    text = value.casefold()
    policy_terms = (
        "all questions required",
        "all questions are required",
        "all requirements required",
        "all requirements are required",
        "every question must",
        "every requirement must",
        "majority",
    )
    return any(term in text for term in policy_terms) and "?" not in value


def _stable_requirement_id(text: str, used: set[str]) -> str:
    base = slugify(text)[:48].replace("-", "_") or "requirement"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base[:44]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _question_for_requirement(text: str) -> tuple[str, bool]:
    cleaned = text.strip()
    if cleaned.endswith("?"):
        return cleaned, True
    return cleaned, False


def compile_qualification_requirements(raw: str) -> dict[str, Any]:
    mode = _qualification_mode(raw)
    used: set[str] = set()
    requirements: list[dict[str, Any]] = []

    for item in _split_requirement_text(raw):
        if _is_policy_line(item):
            continue
        question, can_direct_ask = _question_for_requirement(item)
        required = "optional" not in item.casefold()
        requirements.append(
            {
                "id": _stable_requirement_id(item, used),
                "label": item.rstrip("?. "),
                "question": question,
                "required": required,
                "priority": len(requirements) + 1,
                "can_direct_ask": can_direct_ask,
            }
        )

    return {
        "mode": mode,
        "requirements": requirements,
        "raw": str(raw or "").strip(),
    }


def _languages(raw: str) -> list[str]:
    values = re.split(r"[,;/\n]+", str(raw or ""))
    return [value.strip() for value in values if value.strip()]


def _empty_profile(organization_name: str) -> dict[str, Any]:
    return {
        "version": PROFILE_VERSION,
        "identity": {"name": organization_name, "about": ""},
        "communication": {"languages": [], "custom_instructions": ""},
        "qualification": {"mode": "configured", "requirements": [], "raw": ""},
        "knowledge_policy": {
            "source": "rag_only",
            "unknown_fact": "human_confirmation",
        },
        "instruction_precedence": [
            "platform_rules",
            "organization_policy",
            "pipeline_stage_rules",
            "current_crm_state",
            "verified_knowledge",
            "recent_conversation",
            "rolling_summary",
            "historical_notes",
        ],
    }


def compile_org_ai_profile(*, organization_name: str, org_info) -> dict[str, Any]:
    """Compile existing OrgInfo fields into a deterministic runtime profile."""
    if org_info is None:
        return _empty_profile(organization_name)

    updated_at = getattr(org_info, "updated_at", None)
    cache_key = (
        f"shvya:ai:org-profile:{getattr(org_info, 'pk', 'none')}:"
        f"{updated_at.isoformat() if updated_at else 'na'}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return deepcopy(cached)

    profile = {
        "version": PROFILE_VERSION,
        "identity": {
            "name": organization_name,
            "about": str(getattr(org_info, "about", "") or "").strip(),
        },
        "communication": {
            "languages": _languages(getattr(org_info, "bot_languages", "")),
            "custom_instructions": str(
                getattr(org_info, "engagement_instructions", "") or ""
            ).strip(),
        },
        "qualification": compile_qualification_requirements(
            getattr(org_info, "qualification_requirements", "")
        ),
        "knowledge_policy": {
            "source": "rag_only",
            "unknown_fact": "human_confirmation",
        },
        "instruction_precedence": _empty_profile(organization_name)[
            "instruction_precedence"
        ],
    }
    cache.set(cache_key, profile, PROFILE_CACHE_SECONDS)
    return deepcopy(profile)


def compile_org_ai_profile_from_context(organization_context: dict[str, Any]) -> dict[str, Any]:
    """Compile the same profile from AIContext's legacy OrgInfo fields."""
    context = organization_context if isinstance(organization_context, dict) else {}
    proxy = SimpleNamespace(
        pk=None,
        updated_at=None,
        about=context.get("about", ""),
        bot_languages=context.get("bot_languages", ""),
        qualification_requirements=context.get("qualification_requirements", ""),
        engagement_instructions=context.get("engagement_instructions", ""),
    )
    return compile_org_ai_profile(
        organization_name=str(context.get("name") or ""),
        org_info=proxy,
    )
