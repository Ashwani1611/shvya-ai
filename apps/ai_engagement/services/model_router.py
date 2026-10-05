from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


FAST_TIER = "fast"
STANDARD_TIER = "standard"
REASONING_TIER = "reasoning"

_TIER_ENV = {
    FAST_TIER: "OPENAI_FAST_MODEL",
    STANDARD_TIER: "OPENAI_STANDARD_MODEL",
    REASONING_TIER: "OPENAI_REASONING_MODEL",
}

_SIMPLE_ACKS = {
    "ok",
    "okay",
    "thanks",
    "thank you",
    "sure",
    "fine",
    "great",
    "done",
    "got it",
    "understood",
    "yes",
    "no",
}
_SIMPLE_GREETINGS = {
    "hi",
    "hii",
    "hello",
    "hey",
    "namaste",
    "good morning",
    "good afternoon",
    "good evening",
}
_REASONING_TERMS = (
    "compare",
    "comparison",
    "competitor",
    "cheaper",
    "expensive",
    "why should",
    "why would",
    "difference",
    "different",
    "recommend",
    "best option",
    "which plan",
    "discount",
    "negotiate",
    "negotiation",
    "refund",
    "cancel",
    "cancellation",
    "contract",
    "guarantee",
    "migration",
    "integrate",
    "integration",
    "custom requirement",
    "customization",
    "customisation",
    "not interested",
    "too costly",
    "too expensive",
)


@dataclass(frozen=True)
class ModelRoute:
    model: str
    tier: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "model": self.model,
            "tier": self.tier,
            "reason": self.reason,
        }


def _enabled() -> bool:
    raw = str(os.getenv("OPENAI_ADAPTIVE_ROUTING_ENABLED", "true") or "").strip().casefold()
    return raw not in {"0", "false", "no", "off"}


def _configured_model(tier: str) -> str:
    value = str(os.getenv(_TIER_ENV.get(tier, ""), "") or "").strip()
    if value and (len(value) > 100 or any(character.isspace() for character in value)):
        return ""
    return value


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _latest_inbound_from_messages(messages) -> str:
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("direction") != "inbound":
            continue
        text = str(item.get("body") or "").strip()
        if text:
            return text
    return ""


def _customer_text(input_text: str) -> str:
    """Extract the customer utterance from SHVYA's structured provider payload.

    Routing on the entire JSON payload would incorrectly classify every rich turn
    as complex because AI Brain, RAG and CRM context can be large. Only the latest
    customer text should decide whether the response needs a stronger model.
    """

    raw = str(input_text or "").strip()
    if not raw:
        return ""

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw[:4000]

    def from_mapping(value) -> str:
        if not isinstance(value, dict):
            return ""
        for key in (
            "latest_inbound",
            "latest_customer_message",
            "latest_text",
            "customer_message",
            "message",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        for key in ("recent_conversation", "conversation"):
            nested = value.get(key)
            if isinstance(nested, dict):
                candidate = _latest_inbound_from_messages(nested.get("messages"))
                if candidate:
                    return candidate

        candidate = _latest_inbound_from_messages(value.get("messages"))
        if candidate:
            return candidate

        original = value.get("original_turn")
        if isinstance(original, dict):
            candidate = from_mapping(original)
            if candidate:
                return candidate
        return ""

    return (from_mapping(payload) or raw)[:4000]


def _feature(metadata: dict[str, str] | None) -> str:
    metadata = metadata or {}
    raw = metadata.get("task") or metadata.get("purpose") or metadata.get("feature") or "other"
    value = str(raw).strip().casefold().replace(" ", "_")
    aliases = {
        "lead_qualification_summary": "qualification",
        "internal_conversation_summary": "internal_summary",
        "bump_up": "bump_up",
        "intent_score": "intent_score",
    }
    return aliases.get(value, value[:64] or "other")


def classify_tier(*, input_text: str, metadata: dict[str, str] | None = None) -> tuple[str, str]:
    metadata = metadata or {}
    feature = _feature(metadata)
    phase = _normalized(metadata.get("phase"))
    prompt_mode = _normalized(metadata.get("prompt_mode"))

    # Background/extraction tasks do not need to compete with customer-facing
    # sales reasoning for the strongest model.
    if feature in {"internal_summary", "lead_briefing", "bump_up", "intent_score"}:
        return FAST_TIER, "background_or_extraction"

    if phase in {"intent_classification", "summary", "background_enrichment"}:
        return FAST_TIER, "classification_or_background"

    # A malformed structured decision is worth repairing with the strongest
    # configured tier so one bad JSON envelope does not strand the live turn.
    if phase in {"schema_repair", "grounding_repair"}:
        return REASONING_TIER, "structured_repair"

    text = _customer_text(input_text)
    normalized = _normalized(text)
    if not normalized:
        return STANDARD_TIER, "no_customer_text"

    if normalized.strip(" .!?") in (_SIMPLE_ACKS | _SIMPLE_GREETINGS):
        return FAST_TIER, "simple_ack_or_greeting"

    question_count = text.count("?")
    sentence_count = len([item for item in re.split(r"[.!?]+", text) if item.strip()])
    word_count = len(re.findall(r"\b\w+\b", text, flags=re.UNICODE))

    if any(term in normalized for term in _REASONING_TERMS):
        return REASONING_TIER, "sales_objection_or_comparison"
    if question_count >= 2:
        return REASONING_TIER, "multiple_questions"
    if word_count >= 90 or (word_count >= 55 and sentence_count >= 4):
        return REASONING_TIER, "long_multi_part_message"
    if prompt_mode == "sales_support" and word_count >= 45:
        return REASONING_TIER, "complex_sales_support"

    return STANDARD_TIER, "normal_customer_turn"


def route_model(
    *,
    base_model: str,
    input_text: str,
    metadata: dict[str, str] | None = None,
) -> ModelRoute:
    """Choose a platform-controlled model tier without adding another AI call.

    Organization model overrides are intentionally handled before this function
    by OpenAIProvider. This router therefore cannot grant tenants model authority.
    """

    base_model = str(base_model or "").strip()
    if not _enabled():
        return ModelRoute(model=base_model, tier=STANDARD_TIER, reason="adaptive_routing_disabled")

    tier, reason = classify_tier(input_text=input_text, metadata=metadata)
    configured = _configured_model(tier)

    # Reasoning may fall back to the standard configured model when an operator
    # has enabled adaptive routing but has not assigned a dedicated reasoning
    # model. No hard-coded provider model is introduced here.
    if not configured and tier == REASONING_TIER:
        configured = _configured_model(STANDARD_TIER)
    if not configured and tier == STANDARD_TIER:
        configured = _configured_model(FAST_TIER)

    return ModelRoute(
        model=configured or base_model,
        tier=tier,
        reason=reason,
    )


def fallback_models(
    *,
    primary_model: str,
    base_model: str,
    tier: str,
) -> tuple[str, ...]:
    """Return operator-controlled fallback candidates in safe downgrade order."""

    values: list[str] = []

    explicit = str(os.getenv("OPENAI_FALLBACK_MODEL", "") or "").strip()
    if explicit and len(explicit) <= 100 and not any(character.isspace() for character in explicit):
        values.append(explicit)

    if tier == REASONING_TIER:
        values.extend((_configured_model(STANDARD_TIER), _configured_model(FAST_TIER)))
    elif tier == STANDARD_TIER:
        values.append(_configured_model(FAST_TIER))

    values.append(str(base_model or "").strip())

    primary = str(primary_model or "").strip()
    output: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if not value or value == primary or value in output:
            continue
        output.append(value)
    return tuple(output)
