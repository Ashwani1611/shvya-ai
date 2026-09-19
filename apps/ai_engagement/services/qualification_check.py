from __future__ import annotations

import json
from dataclasses import dataclass

from apps.ai_engagement.prompts.qualification_check import (
    QUALIFICATION_CHECK_INSTRUCTIONS,
    SEMANTIC_CRITERIA_INSTRUCTIONS,
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
    """Internal qualification checks and evidence-bound background verification.

    Normal engagement stays state-driven. The background qualifier uses the
    semantic method only after collection completes and authored criteria remain
    unresolved; current signed receipts avoid repeated verification calls.
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
                    "ai_playbook": organization.get("ai_playbook", ""),
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

    def evaluate_criteria(self, *, clauses, evidence, backend_verdicts, organization, lead) -> dict:
        """One bounded model call; truth/evidence and receipt validation stay local."""
        from apps.ai_engagement.services.ai_provider import AIProviderTransientError
        provider = self.provider or OpenAIProvider()
        try:
            result = provider.generate_text(
                instructions=SEMANTIC_CRITERIA_INSTRUCTIONS,
                input_text=json.dumps({"criteria": clauses, "answers": evidence,
                                       "backend_verdicts": backend_verdicts}, ensure_ascii=False),
                metadata={"organization_id": str(organization.id), "lead_id": str(lead.id),
                          "task": "qualification", "purpose": "playbook_criteria_verification"},
            )
        except AIProviderTransientError:
            raise
        except AIProviderError as exc:
            raise QualificationCheckError("Semantic qualification verification failed.") from exc
        try:
            payload = json.loads(result.text)
        except (ValueError, TypeError) as exc:
            raise QualificationCheckError("Semantic qualification returned invalid JSON.") from exc
        return payload

    def refresh_semantic(self, *, organization, lead) -> dict:
        from apps.ai_engagement.services.semantic_criteria import refresh_semantic_criteria
        return refresh_semantic_criteria(service=self, organization=organization, lead=lead)
