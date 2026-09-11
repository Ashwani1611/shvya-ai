from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from django.core.cache import cache
from django.utils.text import slugify


PROFILE_VERSION = 2
PROFILE_CACHE_SECONDS = 300

_OPTION_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?P<key>[A-Za-z]|\d{1,2})\s*[\)\].:\-]\s+(?P<value>.+?)\s*$"
)
_INLINE_OPTION_RE = re.compile(
    r"(?<!\w)(?P<key>[A-Za-z]|\d{1,2})\s*[\)\].:\-]\s+"
)
_EXPLICIT_ID_RE = re.compile(r"^\s*\[id\s*:\s*(?P<id>[A-Za-z0-9_-]+)\]\s*", re.IGNORECASE)


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


def _normalize_option_key(value: str) -> str:
    text = str(value or "").strip()
    return text.upper() if text.isalpha() else text


def _looks_like_question(value: str) -> bool:
    text = _clean_requirement_line(value)
    return bool(text) and (
        text.endswith("?")
        or bool(re.match(r"^(?:what|which|where|who|how|is|are|do|does|did|have|has|can|could|would|will|select|choose|share|tell)\b", text, re.IGNORECASE))
    )


def _extract_inline_options(value: str) -> tuple[str, list[dict[str, str]]] | None:
    text = str(value or "").strip()
    matches = list(_INLINE_OPTION_RE.finditer(text))
    if len(matches) < 2:
        return None
    prefix = text[: matches[0].start()].strip(" :-")
    options: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        option_value = text[start:end].strip(" ,;|/")
        if option_value:
            options.append({
                "key": _normalize_option_key(match.group("key")),
                "value": re.sub(r"\s+", " ", option_value).strip(),
            })
    if len(options) < 2:
        return None
    return prefix, options


def _append_unique_option(block: dict[str, Any], key: str, value: str) -> None:
    normalized_key = _normalize_option_key(key)
    normalized_value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not normalized_value:
        return
    markers = {
        (_normalize_option_key(item.get("key", "")), str(item.get("value", "")).casefold())
        for item in block["options"]
    }
    marker = (normalized_key, normalized_value.casefold())
    if marker not in markers:
        block["options"].append({"key": normalized_key, "value": normalized_value})


def _requirement_blocks(raw: str) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return []

    compact = None
    if "\n" not in text and ";" not in text and "?" not in text:
        compact = _split_compact_list(text)
    if compact:
        return [{"text": item, "options": []} for item in compact]

    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_part in re.split(r"[\n;]+", text):
        original = str(raw_part or "").strip()
        if not original:
            continue

        inline = _extract_inline_options(original)
        if inline is not None:
            prefix, options = inline
            if prefix:
                current = {"text": _clean_requirement_line(prefix), "options": []}
                for option in options:
                    _append_unique_option(current, option["key"], option["value"])
                blocks.append(current)
            elif current is not None:
                for option in options:
                    _append_unique_option(current, option["key"], option["value"])
            continue

        option_match = _OPTION_LINE_RE.match(original)
        if option_match is not None and current is not None:
            option_value = re.sub(r"\s+", " ", option_match.group("value")).strip()
            if not option_value.endswith("?") and (current["options"] or _looks_like_question(current["text"])):
                _append_unique_option(current, option_match.group("key"), option_value)
                continue

        current = {"text": _clean_requirement_line(original), "options": []}
        if current["text"]:
            blocks.append(current)
    return blocks


def _split_requirement_text(raw: str) -> list[str]:
    """Legacy helper retained for callers/tests; option-aware compilation uses blocks."""
    return [block["text"] for block in _requirement_blocks(raw) if block.get("text")]


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
    """Legacy wording-based id retained as a compatibility alias."""
    base = slugify(text)[:48].replace("-", "_") or "requirement"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base[:44]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _explicit_requirement_id(text: str) -> tuple[str | None, str]:
    match = _EXPLICIT_ID_RE.match(text)
    if not match:
        return None, text
    explicit = slugify(match.group("id")).replace("-", "_")[:64] or None
    return explicit, text[match.end():].strip()


def _flow_version(mode: str, requirements: list[dict[str, Any]]) -> str:
    payload = {
        "mode": mode,
        "requirements": [
            {
                "stable_id": item["stable_id"],
                "text": item["question"],
                "required": item["required"],
                "options": item.get("options", []),
            }
            for item in requirements
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _question_for_requirement(text: str) -> tuple[str, bool]:
    cleaned = text.strip()
    return cleaned, bool(cleaned)


def compile_qualification_requirements(raw: str) -> dict[str, Any]:
    """Compile free-text AI Setup into a deterministic, versioned flow.

    `id` remains the legacy authored-text slug for API/test compatibility.
    `stable_id` is the persisted identity and is independent of wording. Existing
    organizations may optionally author `[id: budget]` before a requirement; when
    omitted, stable ids are positional (`qualification_1`, ...). In-progress leads
    additionally pin a flow snapshot in qualification_state so wording/config edits
    cannot silently restart a live questionnaire.
    """
    mode = _qualification_mode(raw)
    used_legacy: set[str] = set()
    used_stable: set[str] = set()
    requirements: list[dict[str, Any]] = []

    for block in _requirement_blocks(raw):
        authored = re.sub(r"\s+", " ", str(block.get("text") or "")).strip()
        if not authored or _is_policy_line(authored):
            continue

        explicit_id, text = _explicit_requirement_id(authored)
        if not text:
            continue
        legacy_id = _stable_requirement_id(text, used_legacy)
        stable_base = explicit_id or f"qualification_{len(requirements) + 1}"
        stable_id = stable_base
        suffix = 2
        while stable_id in used_stable:
            stable_id = f"{stable_base}_{suffix}"
            suffix += 1
        used_stable.add(stable_id)

        options = [
            {
                "key": _normalize_option_key(item.get("key", "")),
                "value": re.sub(r"\s+", " ", str(item.get("value") or "")).strip(),
            }
            for item in block.get("options") or []
            if str(item.get("value") or "").strip()
        ]
        question, can_direct_ask = _question_for_requirement(text)
        if options:
            question = f"{question}\n" + "\n".join(
                f"{item['key']}. {item['value']}" for item in options
            )

        required = "optional" not in text.casefold()
        if mode == "all_required":
            required = True

        requirements.append({
            "id": legacy_id,
            "stable_id": stable_id,
            "legacy_ids": [legacy_id],
            "label": text.rstrip("?. "),
            "question": question,
            "required": required,
            "priority": len(requirements) + 1,
            "can_direct_ask": can_direct_ask or bool(options),
            "options": options,
        })

    version = _flow_version(mode, requirements) if requirements else ""
    for requirement in requirements:
        requirement["flow_version"] = version

    return {
        "mode": mode,
        "flow_version": version,
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
        "qualification": {
            "mode": "configured",
            "flow_version": "",
            "requirements": [],
            "raw": "",
        },
        "knowledge_policy": {
            "source": "rag_only",
            "unknown_fact": "human_confirmation",
        },
        "instruction_precedence": [
            "platform_rules",
            "backend_qualification_state",
            "organization_policy",
            "pipeline_stage_rules",
            "current_crm_state",
            "verified_knowledge",
            "recent_conversation",
            "rolling_summary",
            "historical_notes",
        ],
    }


def _profile_from_values(
    *,
    organization_name: str,
    about: str,
    bot_languages: str,
    qualification_requirements: str,
    engagement_instructions: str,
) -> dict[str, Any]:
    return {
        "version": PROFILE_VERSION,
        "identity": {
            "name": organization_name,
            "about": str(about or "").strip(),
        },
        "communication": {
            "languages": _languages(bot_languages),
            "custom_instructions": str(engagement_instructions or "").strip(),
        },
        "qualification": compile_qualification_requirements(qualification_requirements),
        "knowledge_policy": {
            "source": "rag_only",
            "unknown_fact": "human_confirmation",
        },
        "instruction_precedence": _empty_profile(organization_name)["instruction_precedence"],
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

    profile = _profile_from_values(
        organization_name=organization_name,
        about=getattr(org_info, "about", ""),
        bot_languages=getattr(org_info, "bot_languages", ""),
        qualification_requirements=getattr(org_info, "qualification_requirements", ""),
        engagement_instructions=getattr(org_info, "engagement_instructions", ""),
    )
    cache.set(cache_key, profile, PROFILE_CACHE_SECONDS)
    return deepcopy(profile)


def compile_org_ai_profile_from_context(organization_context: dict[str, Any]) -> dict[str, Any]:
    """Compile from AIContext without cross-organization cache reuse."""
    context = organization_context if isinstance(organization_context, dict) else {}
    return _profile_from_values(
        organization_name=str(context.get("name") or ""),
        about=str(context.get("about") or ""),
        bot_languages=str(context.get("bot_languages") or ""),
        qualification_requirements=str(context.get("qualification_requirements") or ""),
        engagement_instructions=str(context.get("engagement_instructions") or ""),
    )
