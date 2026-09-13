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

    MESSAGE_LIMIT = 100
    KNOWLEDGE_LIMIT = 3
    NOTE_LIMIT = 5

    def __init__(self, *, provider=None, context_builder=None) -> None:
        self.provider = provider
        self.context_builder = context_builder or AIContextBuilder()

    @staticmethod
    def _build_input(data: dict) -> str:
        organization = data.get("organization") or {}
        lead = data.get("lead") or {}
        return json.dumps(
            {
                "organization": {
                    "name": organization.get("name", ""),
                    "about": organization.get("about", ""),
                    "bot_languages": organization.get("bot_languages", ""),
                    "qualification_requirements": organization.get(
                        "qualification_requirements", ""
                    ),
                    "engagement_instructions": organization.get(
                        "engagement_instructions", ""
                    ),
                },
                "qualification_state": lead.get("qualification") or {},
                "lead": lead,
                "recent_conversation": data.get("conversation") or {},
                "conversation_summary": data.get("conversation_summary"),
            },
            ensure_ascii=False,
        )

    def check(self, *, organization, lead) -> QualificationCheckResult:
        context = self.context_builder.build(
            organization=organization,
            lead=lead,
            message_limit=self.MESSAGE_LIMIT,
            knowledge_limit=self.KNOWLEDGE_LIMIT,
            note_limit=self.NOTE_LIMIT,
        )
        data = context.as_dict()
        input_text = self._build_input(data)
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
