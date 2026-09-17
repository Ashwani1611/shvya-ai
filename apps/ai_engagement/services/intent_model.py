from __future__ import annotations

import json
from typing import Any

from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.intent_types import (
    ClassificationPath,
    Intent,
    IntentDecision,
    IntentError,
)


CANONICAL = [item.value for item in Intent]
SCHEMA = {
    "name": "shvya_intent_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "primary_intent": {"type": "string", "enum": CANONICAL},
            "secondary_intents": {"type": "array", "items": {"type": "string", "enum": CANONICAL}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "entities": {"type": "array", "items": {"type": "object", "properties": {"type": {"type": "string"}, "value": {"type": "string"}}, "required": ["type", "value"], "additionalProperties": False}},
            "facts": {"type": "array", "items": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["key", "value", "evidence"], "additionalProperties": False}},
            "direct_question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "qualification_candidate": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {"requirement_id": {"type": "string"}, "value": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["requirement_id", "value", "evidence"], "additionalProperties": False}]},
            "requested_action": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "language": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "requires_knowledge": {"type": "boolean"},
            "requires_human": {"type": "boolean"},
        },
        "required": ["primary_intent", "secondary_intents", "confidence", "entities", "facts", "direct_question", "qualification_candidate", "requested_action", "language", "requires_knowledge", "requires_human"],
        "additionalProperties": False,
    },
}
INSTRUCTIONS = """Classify only the latest customer message for SHVYA. Return the strict schema using only canonical intent enums. Multiple intents are allowed. Preserve a direct customer question even if the same message answers qualification. QUALIFICATION_ANSWER is only an observation against supplied organization requirements and never authorizes a write. AMBIGUOUS means several meanings need context; UNKNOWN means no supported intent is identifiable. Never invent business-specific global intents or choose tenant identity, CRM mutations, stages, notes, reminders, or delivery. Use only supplied organization-scoped requirements/state."""


def call_model(*, provider, organization, lead, text, source_message_id, requirements, state) -> IntentDecision:
    payload = {
        "message": text,
        "qualification_requirements": [
            {"id": str(item.get("id") or ""), "question": str(item.get("question") or item.get("label") or "")}
            for item in requirements if str(item.get("id") or "").strip()
        ],
        "qualification_state": {key: state.get(key) for key in ("current_requirement_id", "last_asked_requirement_id", "next_requirement_id")},
    }
    result = provider.generate_text(
        instructions=INSTRUCTIONS,
        input_text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        metadata={
            "organization_id": str(getattr(organization, "id", "") or ""),
            "lead_id": str(getattr(lead, "id", "") or ""),
            "source_message_id": str(source_message_id or ""),
            "task": "engagement",
            "phase": "intent_classification",
        },
        response_schema=SCHEMA,
    )
    return parse_result(result)


def parse_result(result: AITextResult) -> IntentDecision:
    payload = json.loads(result.text)
    if not isinstance(payload, dict):
        raise IntentError("Intent provider output must be an object.")
    primary = _intent(payload.get("primary_intent"))
    raw_secondary = payload.get("secondary_intents")
    if not isinstance(raw_secondary, list):
        raise IntentError("secondary_intents must be a list.")
    secondary = tuple(item for item in (_intent(value) for value in raw_secondary) if item != primary)
    confidence = float(payload.get("confidence"))
    if not 0 <= confidence <= 1:
        raise IntentError("Intent confidence must be between zero and one.")
    entities = _objects(payload.get("entities"), ("type", "value"))
    facts = _objects(payload.get("facts"), ("key", "value", "evidence"))
    candidate = payload.get("qualification_candidate")
    if candidate is not None and (not isinstance(candidate, dict) or not all(key in candidate for key in ("requirement_id", "value", "evidence"))):
        raise IntentError("Invalid qualification_candidate shape.")
    for key in ("direct_question", "requested_action", "language"):
        if payload.get(key) is not None and not isinstance(payload.get(key), str):
            raise IntentError(f"{key} must be a string or null.")
    if not isinstance(payload.get("requires_knowledge"), bool) or not isinstance(payload.get("requires_human"), bool):
        raise IntentError("Intent flags must be booleans.")
    return IntentDecision(
        primary_intent=primary,
        secondary_intents=secondary,
        confidence=confidence,
        entities=tuple(entities),
        facts=tuple(facts),
        direct_question=payload.get("direct_question"),
        qualification_candidate=dict(candidate) if isinstance(candidate, dict) else None,
        requested_action=payload.get("requested_action"),
        classification_path=ClassificationPath.MODEL,
        language=payload.get("language"),
        requires_knowledge=payload["requires_knowledge"],
        requires_human=payload["requires_human"],
        model=str(result.model or ""),
    )


def _objects(value: Any, required: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise IntentError("Structured intent array is invalid.")
    result = []
    for item in value:
        if not isinstance(item, dict) or not all(key in item for key in required):
            raise IntentError("Structured intent item is invalid.")
        result.append(dict(item))
    return result


def _intent(value: Any) -> Intent:
    try:
        return Intent(str(value or ""))
    except ValueError as exc:
        raise IntentError("Provider returned an unsupported intent value.") from exc
