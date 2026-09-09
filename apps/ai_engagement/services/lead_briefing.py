from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from apps.ai_engagement.prompts.lead_briefing import LEAD_BRIEFING_INSTRUCTIONS
from apps.ai_engagement.services.ai_provider import AIProviderError, OpenAIProvider
from apps.ai_engagement.services.context import AIContextBuilder


class LeadBriefingError(Exception):
    pass


@dataclass(frozen=True)
class LeadBriefingResult:
    notes: str
    attributes: dict[str, Any]
    intent_score: int
    high_priority_lead: bool
    model: str


class LeadBriefingService:
    """Generate the historical bump-up document's internal sales briefing.

    It is intentionally on-demand, not part of every inbound-message path, so
    SHVYA does not spend an extra model call merely to calculate internal sales
    metadata before replying to the customer.
    """

    def __init__(self, *, provider=None, context_builder=None) -> None:
        self.provider = provider
        self.context_builder = context_builder or AIContextBuilder()

    def generate(self, *, organization, lead) -> LeadBriefingResult:
        context = self.context_builder.build(
            organization=organization,
            lead=lead,
            message_limit=24,
            knowledge_limit=3,
            note_limit=5,
        )
        data = context.as_dict()
        input_text = json.dumps(
            {
                "organization": data["organization"],
                "lead": data["lead"],
                "attributes": data["attributes"],
                "conversation_summary": data["conversation_summary"],
                "recent_conversation": data["conversation"],
            },
            ensure_ascii=False,
        )
        provider = self.provider or OpenAIProvider()
        try:
            result = provider.generate_text(
                instructions=LEAD_BRIEFING_INSTRUCTIONS,
                input_text=input_text,
                metadata={
                    "organization_id": str(organization.id),
                    "lead_id": str(lead.id),
                    "task": "lead_briefing",
                },
            )
        except AIProviderError as exc:
            raise LeadBriefingError("AI lead briefing failed.") from exc

        try:
            payload = json.loads((result.text or "").strip())
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LeadBriefingError("Lead briefing returned invalid JSON.") from exc
        expected = {"notes", "attributes", "intent_score", "high_priority_lead"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise LeadBriefingError("Lead briefing returned an invalid schema.")
        if not isinstance(payload["notes"], str):
            raise LeadBriefingError("notes must be a string.")
        if not isinstance(payload["attributes"], dict):
            raise LeadBriefingError("attributes must be an object.")
        if isinstance(payload["intent_score"], bool) or not isinstance(
            payload["intent_score"], int
        ):
            raise LeadBriefingError("intent_score must be an integer.")
        if not 0 <= payload["intent_score"] <= 10:
            raise LeadBriefingError("intent_score must be between 0 and 10.")
        if not isinstance(payload["high_priority_lead"], bool):
            raise LeadBriefingError("high_priority_lead must be boolean.")

        return LeadBriefingResult(
            notes=payload["notes"].strip()[:300],
            attributes=payload["attributes"],
            intent_score=payload["intent_score"],
            high_priority_lead=payload["high_priority_lead"],
            model=result.model,
        )
