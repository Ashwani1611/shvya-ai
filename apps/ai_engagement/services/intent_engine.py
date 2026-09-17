from __future__ import annotations

import json
from typing import Any, Iterable

from apps.ai_engagement.services.ai_provider import AIProviderError, OpenAIProvider
from apps.ai_engagement.services.intent_model import call_model
from apps.ai_engagement.services.intent_rules import (
    EXACT_GREETING,
    EXACT_THANKS,
    KNOWLEDGE_INTENTS,
    detect_language,
    deterministic_intents,
    direct_question,
    normalize,
    ordered_intents,
    preferred_candidate,
    qualification_facts,
    requested_action,
)
from apps.ai_engagement.services.intent_types import (
    ClassificationPath,
    Intent,
    IntentDecision,
    IntentError,
    IntentScopeError,
)


class IntentEngine:
    """Reusable, organization-independent and side-effect-free intent engine."""

    def __init__(self, *, provider: OpenAIProvider | None = None) -> None:
        self.provider = provider

    def classify(self, *, organization, lead, message: str, source_message_id: str | None = None,
                 requirements: Iterable[dict[str, Any]] | None = None,
                 qualification_state: dict[str, Any] | None = None, context=None) -> IntentDecision:
        self._validate_scope(organization=organization, lead=lead, context=context)
        text = str(message or "").strip()
        if not text:
            return IntentDecision(primary_intent=Intent.UNKNOWN, confidence=1.0, classification_path=ClassificationPath.UNKNOWN)
        reqs = [dict(item) for item in (requirements or []) if isinstance(item, dict)]
        state = dict(qualification_state or {})
        language = detect_language(text)
        question = direct_question(text)
        facts = qualification_facts(text=text, requirements=reqs, qualification_state=state, source_message_id=source_message_id)
        intents = deterministic_intents(text)
        if facts:
            intents.add(Intent.QUALIFICATION_ANSWER)
        if not intents or (question and intents.issubset({Intent.QUALIFICATION_ANSWER})):
            return self._model_fallback(
                organization=organization, lead=lead, text=text, source_message_id=source_message_id,
                requirements=reqs, state=state, deterministic_facts=facts, question=question, language=language,
            )
        primary, secondary = ordered_intents(intents)
        confidence = 0.94
        if Intent.OPT_OUT in intents or normalize(text).strip(" .!?") in EXACT_GREETING | EXACT_THANKS:
            confidence = 0.99
        if facts:
            confidence = max(0.9, max(float(item.get("confidence") or 0) for item in facts))
        return IntentDecision(
            primary_intent=primary, secondary_intents=secondary, confidence=confidence, facts=tuple(facts),
            direct_question=question, qualification_candidate=preferred_candidate(facts, state),
            requested_action=requested_action(intents), classification_path=ClassificationPath.DETERMINISTIC,
            language=language, requires_knowledge=any(item in KNOWLEDGE_INTENTS for item in intents),
            requires_human=Intent.HUMAN_REQUEST in intents or Intent.CALL_REQUEST in intents, model="deterministic",
        )

    @staticmethod
    def _validate_scope(*, organization, lead, context) -> None:
        if organization is None or lead is None:
            raise IntentScopeError("Organization and lead are required.")
        org_id = str(getattr(organization, "id", "") or "")
        lead_org_id = str(getattr(lead, "organization_id", "") or "")
        if lead_org_id and lead_org_id != org_id:
            raise IntentScopeError("Lead does not belong to the supplied organization.")
        if context is None:
            return
        context_org = getattr(context, "organization", None)
        context_lead = getattr(context, "lead", None)
        context_org_id = str((context_org or {}).get("id") or "") if isinstance(context_org, dict) else ""
        context_lead_id = str((context_lead or {}).get("id") or "") if isinstance(context_lead, dict) else ""
        if context_org_id and context_org_id != org_id:
            raise IntentScopeError("AI context organization scope mismatch.")
        lead_id = str(getattr(lead, "id", "") or "")
        if context_lead_id and lead_id and context_lead_id != lead_id:
            raise IntentScopeError("AI context lead scope mismatch.")

    def _model_fallback(self, *, organization, lead, text, source_message_id, requirements, state,
                        deterministic_facts, question, language) -> IntentDecision:
        try:
            parsed = call_model(
                provider=self.provider or OpenAIProvider(), organization=organization, lead=lead, text=text,
                source_message_id=source_message_id, requirements=requirements, state=state,
            )
        except (AIProviderError, IntentError, json.JSONDecodeError, TypeError, ValueError) as exc:
            if deterministic_facts:
                return IntentDecision(
                    primary_intent=Intent.QUALIFICATION_ANSWER,
                    confidence=max(float(item.get("confidence") or 0) for item in deterministic_facts),
                    facts=tuple(deterministic_facts), direct_question=question,
                    qualification_candidate=preferred_candidate(deterministic_facts, state),
                    classification_path=ClassificationPath.FALLBACK, language=language,
                    classification_error=exc.__class__.__name__,
                )
            return IntentDecision(
                primary_intent=Intent.UNKNOWN, confidence=0.0, direct_question=question,
                classification_path=ClassificationPath.FALLBACK, language=language,
                classification_error=exc.__class__.__name__,
            )
        model_intents = {parsed.primary_intent, *parsed.secondary_intents}
        if deterministic_facts:
            model_intents.add(Intent.QUALIFICATION_ANSWER)
        primary, secondary = ordered_intents(model_intents)
        facts = self._merge_facts(deterministic_facts, list(parsed.facts))
        return IntentDecision(
            primary_intent=primary, secondary_intents=secondary, confidence=parsed.confidence,
            entities=parsed.entities, facts=tuple(facts), direct_question=parsed.direct_question or question,
            qualification_candidate=preferred_candidate(facts, state) or parsed.qualification_candidate,
            requested_action=parsed.requested_action or requested_action(model_intents),
            classification_path=ClassificationPath.MODEL, language=parsed.language or language,
            requires_knowledge=parsed.requires_knowledge or any(i in KNOWLEDGE_INTENTS for i in model_intents),
            requires_human=parsed.requires_human or Intent.HUMAN_REQUEST in model_intents or Intent.CALL_REQUEST in model_intents,
            model=parsed.model,
        )

    @staticmethod
    def _merge_facts(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged, seen = [], set()
        for item in [*first, *second]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("requirement_id") or item.get("key") or json.dumps(item, sort_keys=True, default=str))
            if key not in seen:
                seen.add(key)
                merged.append(dict(item))
        return merged


__all__ = ["ClassificationPath", "Intent", "IntentDecision", "IntentEngine", "IntentError", "IntentScopeError"]
