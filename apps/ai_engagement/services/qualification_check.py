from __future__ import annotations

import json
from dataclasses import dataclass

from apps.ai_engagement.prompts.qualification_check import (
    QUALIFICATION_CHECK_INSTRUCTIONS,
)
from apps.ai_engagement.services.ai_provider import AIProviderError, OpenAIProvider
from apps.ai_engagement.services.context import AIContextBuilder


class QualificationCheckError(Exception):
    pass


@dataclass(frozen=True)
class QualificationCheckResult:
    qualified: bool
    reason: str
    all_questions_answered: bool
    model: str


class QualificationCheckService:
    """On-demand semantic qualification check.

    This service is deliberately not called on every WhatsApp turn. The normal
    engagement path uses persisted application qualification state and one LLM
    call. Use this checker only when an explicit internal verification is
    required.
    """

    def __init__(self, *, provider=None, context_builder=None) -> None:
        self.provider = provider
        self.context_builder = context_builder or AIContextBuilder()

    def check(self, *, organization, lead) -> QualificationCheckResult:
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
                "organization": {
                    "name": data["organization"].get("name", ""),
                    "about": data["organization"].get("about", ""),
                    "qualification_requirements": data["organization"].get(
                        "qualification_requirements", ""
                    ),
                },
                "lead": data["lead"],
                "recent_conversation": data["conversation"],
                "conversation_summary": data["conversation_summary"],
            },
            ensure_ascii=False,
        )
        provider = self.provider or OpenAIProvider()
        try:
            result = provider.generate_text(
                instructions=QUALIFICATION_CHECK_INSTRUCTIONS,
                input_text=input_text,
                metadata={
                    "organization_id": str(organization.id),
                    "lead_id": str(lead.id),
                    "task": "qualification",
                    "purpose": "qualification_check",
                },
            )
        except AIProviderError as exc:
            raise QualificationCheckError("AI qualification check failed.") from exc

        try:
            payload = json.loads((result.text or "").strip())
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise QualificationCheckError("Qualification check returned invalid JSON.") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "qualified",
            "reason",
            "all_questions_answered",
        }:
            raise QualificationCheckError("Qualification check returned an invalid schema.")
        if not isinstance(payload["qualified"], bool):
            raise QualificationCheckError("qualified must be boolean.")
        if not isinstance(payload["all_questions_answered"], bool):
            raise QualificationCheckError("all_questions_answered must be boolean.")
        if not isinstance(payload["reason"], str):
            raise QualificationCheckError("reason must be a string.")

        return QualificationCheckResult(
            qualified=payload["qualified"],
            reason=payload["reason"].strip()[:500],
            all_questions_answered=payload["all_questions_answered"],
            model=result.model,
        )
