"""Shared parsing and state helpers for qualification execution."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


_CONTRACT_KEY = "qualification_execution_contract"
_PLAN_KEY = "qualification_response_plan"

_ACK_LABEL = re.compile(
    r"^\s*(?:[-*•]\s*)?(?P<label>(?:final\s+)?(?:acknowledg(?:e)?ment|completion)\s+message|final\s+acknowledg(?:e)?ment)\s*(?::|=|->|→)\s*(?P<value>.+?)\s*$",
    re.I,
)
_ACK_HEADING_ONLY = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:final\s+)?(?:acknowledg(?:e)?ment|completion)\s+message\s*:?\s*$",
    re.I,
)
_QUESTION_FRAGMENT_RE = re.compile(
    r"^(?:what|which|where|who|how|is|are|do|does|did|have|has|can|could|would|will|"
    r"tell|share|select|choose)\b",
    re.I,
)
_LABEL_ONLY = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:(?:final\s+)?(?:acknowledg(?:e)?ment|completion)\s+message|final\s+acknowledg(?:e)?ment|qualification\s+requirements?|attribute\s+mapped|stage\s+shifting)\s*:?\s*$",
    re.I,
)
_COMPLETION_RULE = re.compile(
    r"\b(?:(?:all\s+)?qualification\s+criteria\s+(?:(?:are|have been)\s+)?(?:satisfied|met|passed)|qualification\s+(?:is\s+)?(?:complete|completed)|(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?(?:questions?|requirements?|answers?)\s+(?:are\s+)?(?:answered|complete|completed)|(?:after|once|when)\s+(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?(?:questions?|requirements?)\s+(?:are\s+)?(?:answered|complete|completed))\b",
    re.I,
)
_GENERIC_ACKS = {
    "got it",
    "great",
    "nice",
    "okay",
    "ok",
    "noted",
    "thanks",
    "thank you",
}
_GREETING_RE = re.compile(r"^[\s*_]*(?:hi|hello|hey|welcome)\b", re.I)

_PLAN_INSTRUCTIONS = """
BACKEND RESPONSE PLAN CONTRACT
- response_plan is backend-authoritative when present.
- qualification_start: output the backend-authored configured requirement/options; do not invent or reorder them.
- qualification_progress: write ONE short personalized acknowledgement reflecting acknowledgement_context. Do not rewrite the next question/options; backend appends them.
- qualification_clarification: briefly ask for clarification without guessing. Backend appends the active configured requirement/options.
- qualification_complete: write ONE short personalized acknowledgement reflecting the final answer. Backend appends the configured final acknowledgement VALUE.
- Never expose configuration labels, requirement ids, attribute keys, stage ids, workflow ids, or execution metadata.
- Never claim an attribute/stage/workflow action succeeded unless reconciled backend state confirms it.
""".strip()

_MAPPING_ERROR_CODES = {
    "invalid_attribute_mapping_rule",
    "unknown_requirement_mapping_reference",
    "unknown_attribute_mapping_reference",
    "ambiguous_attribute_mapping",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value: Any) -> str:
    return _clean(value).casefold()


def _strip_quotes(value: str) -> str:
    value = str(value or "").strip()
    if (
        len(value) > 1
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        return value[1:-1].strip()
    return value


def _processing(message) -> dict[str, Any]:
    payload = message.raw_payload if message and isinstance(message.raw_payload, dict) else {}
    data = payload.get("shvya_ai_processing")
    return deepcopy(data) if isinstance(data, dict) else {}


def _save_processing(message, processing: dict[str, Any]) -> None:
    payload = deepcopy(message.raw_payload) if isinstance(message.raw_payload, dict) else {}
    payload["shvya_ai_processing"] = deepcopy(processing)
    message.raw_payload = payload
    message.save(update_fields=["raw_payload", "updated_at"])


def _reference(value: Any) -> str:
    text = _norm(value).strip("`'\"[](){} “”*")
    return re.sub(
        r"^(?:requirement|question|attribute|field)\s+",
        "",
        text,
    ).strip()


def _requirement_ref(
    value: str,
    requirements: list[dict[str, Any]],
) -> dict[str, Any] | None:
    ref = _reference(value)
    match = re.fullmatch(r"q(?:uestion)?\s*(\d+)", ref)
    if match:
        found = [
            requirement
            for requirement in requirements
            if int(requirement.get("priority") or 0) == int(match.group(1))
        ]
        return found[0] if len(found) == 1 else None

    found = []
    for requirement in requirements:
        aliases = {
            _reference(requirement.get("id")),
            _reference(requirement.get("stable_id")),
            _reference(requirement.get("label")),
            _reference(str(requirement.get("question") or "").splitlines()[0]),
            *(_reference(item) for item in requirement.get("legacy_ids") or []),
        }
        aliases.discard("")
        if ref and ref in aliases:
            found.append(requirement)
    return found[0] if len(found) == 1 else None


def _attribute_ref(
    value: str,
    definitions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    ref = _reference(value)
    found = [
        item
        for item in definitions
        if ref
        and ref
        in {
            _reference(item.get("key")),
            _reference(item.get("name")),
        }
    ]
    return found[0] if len(found) == 1 else None


def _attribute_refs(
    value: str,
    definitions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve authored attribute targets without dropping valid siblings.

    Exact names/keys remain authoritative. Multi-target shorthand is supported
    only when the full right-hand side is not itself an attribute name, so an
    attribute such as "Leads/d" remains one field while authored forms such as
    "Lead Management Tool (+ Using Whatsapp / CRM)" can resolve several fields.

    A missing secondary target must not cancel the valid primary/other targets.
    Missing names are returned separately so configuration diagnostics remain
    visible instead of silently losing all CRM writes for the requirement.
    """
    direct = _attribute_ref(value, definitions)
    if direct is not None:
        return [direct], []

    text = str(value or "").strip()
    text = re.sub(r"\(\s*\+", "+", text)
    text = text.replace(")", " ")
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:\+|/|,|\band\b)\s*", text, flags=re.I)
        if part.strip()
    ]
    if len(parts) < 2:
        return [], ([text] if text else [])

    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = _attribute_ref(part, definitions)
        if item is None:
            unresolved.append(part)
            continue
        key = str(item.get("key") or "")
        if key and key not in seen:
            resolved.append(item)
            seen.add(key)
    return resolved, unresolved


def _split_mapping(line: str) -> tuple[str, str] | None:
    text = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", str(line or "")).strip()
    attribute = re.search(r"(?im)^\s*[-*]?\s*Attribute name:\s*(.+)$", text)
    source = re.search(r"(?im)^\s*[-*]?\s*Source:\s*(?:Qualification\s+)?Q(?:uestion)?\s*(\d+)\b", text)
    if attribute:
        return (f"Q{source.group(1)}", attribute.group(1).strip()) if source else None
    parts = re.split(r"\s*(?:->|=>|→)\s*", text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    match = re.match(r"^\s*map\s+(.+?)\s+to\s+(.+?)\s*$", text, re.I)
    if match:
        return match.group(1), match.group(2)
    match = re.match(r"^\s*(.+?)\s+maps?\s+to\s+(.+?)\s*$", text, re.I)
    return (match.group(1), match.group(2)) if match else None
