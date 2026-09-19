from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from typing import Any

from apps.ai_engagement.services.conversation_policy import (
    ConversationPolicyContext,
    ConversationPolicyDecision,
    ConversationPolicyEngine,
    ConversationPolicyOutcome,
)
from apps.ai_engagement.services.intent_engine import Intent, IntentDecision, IntentEngine
from apps.ai_engagement.services.intent_types import ClassificationPath


_INSTALLED = False
_TURN: ContextVar[dict[str, Any] | None] = ContextVar(
    "shvya_conversation_policy_turn",
    default=None,
)
_POLICY: ContextVar[ConversationPolicyDecision | None] = ContextVar(
    "shvya_conversation_policy_decision",
    default=None,
)

_POLICY_INSTRUCTIONS = """
BACKEND CONVERSATION POLICY CONTRACT
- conversation_policy is backend-authoritative for the conversational strategy on this turn.
- Follow conversation_policy.outcome and allowed_response_goal. Do not choose a
  different overall workflow.
- ANSWER: answer the customer's direct question from permitted organization evidence;
  do not append a qualification question on this turn. For product/service,
  functionality, feature, pricing, policy, location, or availability requests,
  provide the available answer itself; never reply only with an invitation such
  as "Would you like to know more?" or another question.
- For broad feature, functionality, capability, benefit, or "why choose/buy" requests,
  use the retrieved organization evidence to give several concrete supported points.
  Do not merely repeat a one-line product description, and do not invent unsupported claims.
- ASK_QUALIFICATION: ask exactly the backend-selected next_requirement_id and no
  answered requirement.
- ANSWER_THEN_QUALIFY: answer the direct customer question first from permitted
  evidence, then ask exactly the backend-selected next unanswered qualification
  requirement.
- HUMAN_HANDOFF/CALL_HANDOFF: do not continue qualification and do not claim a
  human/call was completed unless validated backend actions confirm it.
- BOOKING_FLOW: never invent slots, dates, availability, or booking confirmation;
  only a validated booking system may confirm a booking.
- OPT_OUT/NO_ACTION: never continue a sales or qualification flow.
- Policy is a response directive only. CRM mutations remain owned by validated backend executors.
""".strip()


def _elapsed_ms(started: float) -> int:
    return max(int((time.perf_counter() - started) * 1000), 0)


def _intent_values(decision: IntentDecision) -> set[Intent]:
    return {decision.primary_intent, *decision.secondary_intents}


def _record_intent(decision: IntentDecision, elapsed_ms: int) -> None:
    try:
        from apps.ai_engagement.services.intent_runtime import _record_trace

        _record_trace(decision=decision, elapsed_ms=elapsed_ms)
    except Exception:
        return


def _record_policy(decision: ConversationPolicyDecision, elapsed_ms: int) -> None:
    try:
        from apps.ai_engagement.services.trace_service import record

        record(
            "policy",
            {
                "outcome": decision.outcome.value,
                "reason_code": decision.reason_code,
                "answer_customer_question": decision.answer_customer_question,
                "continue_qualification": decision.continue_qualification,
                "next_requirement_id": decision.next_requirement_id,
                "requires_knowledge": decision.requires_knowledge,
                "requires_human": decision.requires_human,
                "handoff_type": decision.handoff_type,
                "booking_requested": decision.booking_requested,
                "should_respond": decision.should_respond,
                "allowed_response_goal": decision.allowed_response_goal,
                "qualification_result_reference": (
                    decision.qualification_result_reference
                ),
                "policy_source": decision.policy_source,
                "execution_path": "deterministic_python",
                "debug_reason": decision.explanation_debug_reason,
            },
        )
        record("performance", {"policy_ms": elapsed_ms})
    except Exception:
        return


_AFFIRMATIVE_CONTINUATIONS = {
    "yes", "yes please", "yeah", "yep", "yup", "sure", "okay", "ok", "correct", "right",
}
_INFORMATION_OFFER_TERMS = (
    "detail", "details", "feature", "features", "functionality", "functionalities",
    "capability", "capabilities", "plan", "plans", "pricing", "price", "more about",
    "more information", "tell you more",
)
_INFORMATION_OFFER_PHRASES = (
    "would you like", "do you want", "want to know", "shall i", "can i share",
    "would you want", "like to know", "want details", "want more",
)
_INFORMATION_INTENTS = {
    Intent.PRODUCT_OR_SERVICE_QUESTION,
}
_DETAIL_REQUEST_TERMS = (
    "detail", "details", "functionality", "functionalities", "feature", "features",
    "featurs", "featres", "capability", "capabilities", "benefit", "benefits",
    "advantage", "advantages", "why buy", "why i buy", "why should i buy",
    "why choose", "why should i choose", "what can you do", "what do you offer",
)
_INFORMATION_CTA_PREFIXES = (
    "would you like",
    "do you want",
    "would you want",
    "shall i",
    "can i share",
    "let me know if",
    "which feature",
    "which plan",
    "what would you like",
)
_INFORMATION_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "can", "do", "for", "from",
    "i", "if", "in", "is", "it", "me", "more", "of", "on", "or", "our",
    "the", "to", "we", "what", "which", "with", "would", "you", "your",
}


def _information_reply_has_substance(message: str, *, detailed: bool = False) -> bool:
    """Reject question-only/CTA replies for backend-classified information turns."""
    text = " ".join(str(message or "").strip().split())
    if not text:
        return False

    meaningful: list[str] = []
    parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", str(message or ""))
        if part.strip()
    ]
    for part in parts:
        normalized = " ".join(part.casefold().split()).strip(" \t\r\n")
        if not normalized:
            continue
        if normalized.strip(" .!?;,:\"'") in {
            "sure", "okay", "ok", "absolutely", "certainly", "of course",
        }:
            continue
        if normalized.startswith(_INFORMATION_CTA_PREFIXES):
            continue
        # A standalone question is not an answer to the information request.
        if part.rstrip().endswith("?"):
            continue
        meaningful.extend(
            token
            for token in re.findall(r"\w+", normalized, flags=re.UNICODE)
            if len(token) > 2 and token not in _INFORMATION_STOP_WORDS
        )

    return len(meaningful) >= (10 if detailed else 4)


def _information_detail_requested(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return any(term in normalized for term in _DETAIL_REQUEST_TERMS)


def _normalized_reply(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split()).strip(" .!?;,:\"'")


def _affirmed_information_offer(*, lead, source) -> bool:
    if _normalized_reply(getattr(source, "body", "")) not in _AFFIRMATIVE_CONTINUATIONS:
        return False
    created_at = getattr(source, "created_at", None)
    if created_at is None:
        return False
    query = lead.whatsapp_messages.filter(
        organization_id=lead.organization_id,
        direction="outbound",
        created_at__lt=created_at,
    )
    source_account_id = getattr(source, "account_id", None)
    if source_account_id is not None:
        query = query.filter(account_id=source_account_id)
    previous = query.order_by("-created_at").only("body").first()
    body = " ".join(str(getattr(previous, "body", "") or "").strip().casefold().split())
    if not body:
        return False
    return (
        any(term in body for term in _INFORMATION_OFFER_TERMS)
        and any(phrase in body for phrase in _INFORMATION_OFFER_PHRASES)
    )


def _source_message(*, lead, source_message_id, account_id=None):
    query = lead.whatsapp_messages.filter(
        pk=source_message_id,
        organization_id=lead.organization_id,
        direction="inbound",
    )
    if account_id is not None:
        query = query.filter(account_id=account_id)
    return query.only("id", "body", "raw_payload", "created_at", "account_id").first()


def _classify_turn(
    *,
    organization,
    lead,
    source_message_id,
    requirements,
    qualification_state,
    account_id=None,
) -> IntentDecision | None:
    source = _source_message(
        lead=lead,
        source_message_id=source_message_id,
        account_id=account_id,
    )
    if source is None:
        return None
    payload = source.raw_payload if isinstance(source.raw_payload, dict) else {}
    processing = payload.get("shvya_ai_processing")
    if isinstance(processing, dict) and processing.get("processed") is True:
        # The canonical finalizer owns duplicate handling. Replayed accepted
        # messages need no new intent model call before that existing check.
        return None
    if _affirmed_information_offer(lead=lead, source=source):
        decision = IntentDecision(
            primary_intent=Intent.FOLLOW_UP_RESPONSE,
            confidence=0.99,
            classification_path=ClassificationPath.DETERMINISTIC,
            requires_knowledge=True,
            model="deterministic_context",
        )
        _record_intent(decision, 0)
        return decision
    if _turn_matches(organization=organization, lead=lead, source_message_id=source_message_id):
        observed = (_TURN.get() or {}).get("intent_decision")
        if isinstance(observed, IntentDecision):
            return observed
    from apps.ai_engagement.services.intent_runtime import current_intent_decision
    observed = current_intent_decision(organization_id=organization.pk, lead_id=lead.pk,
                                      source_message_id=source_message_id)
    if isinstance(observed, IntentDecision):
        return observed
    started = time.perf_counter()
    decision = IntentEngine().classify(
        organization=organization,
        lead=lead,
        message=str(source.body or ""),
        source_message_id=str(source.pk),
        requirements=requirements,
        qualification_state=qualification_state,
    )
    _record_intent(decision, _elapsed_ms(started))
    return decision


def _turn_matches(*, organization, lead, source_message_id=None) -> bool:
    turn = _TURN.get()
    if not isinstance(turn, dict):
        return False
    if str(turn.get("organization_id") or "") != str(organization.id):
        return False
    if str(turn.get("lead_id") or "") != str(lead.id):
        return False
    if source_message_id is not None:
        return str(turn.get("source_message_id") or "") == str(source_message_id)
    return True


def _capabilities(organization=None) -> frozenset[str]:
    # SHVYA already has a validated reminder action used by the existing call-request
    # runtime. No canonical booking executor exists in the engagement action contract,
    # so booking is deliberately not advertised here.
    try:
        from apps.ai_engagement.services.organization_runtime_profile import configured_action_types

        allowed = configured_action_types(getattr(organization, "settings", {}))
        return frozenset({"call"}) if "create_reminder" in allowed else frozenset()
    except Exception:
        return frozenset()


def _accepted_result(*, state: dict[str, Any], source_message_id: str) -> dict[str, Any]:
    answered = []
    for requirement_id, item in (state.get("requirement_states") or {}).items():
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("source_message_id") or "") == str(source_message_id)
            and str(item.get("status") or "").casefold() == "answered"
        ):
            answered.append(str(requirement_id))
    return {
        "accepted": bool(answered),
        "source_message_id": str(source_message_id),
        "answered_requirement_ids": answered,
    }


def _build_policy_context(*, organization, lead, turn: dict[str, Any]):
    intent = turn.get("intent_decision")
    if not isinstance(intent, IntentDecision):
        return None
    state = turn.get("qualification_state")
    state = state if isinstance(state, dict) else {}
    result = turn.get("qualification_result")
    result = result if isinstance(result, dict) else {}
    return ConversationPolicyContext(
        intent_decision=intent,
        organization_id=str(organization.id),
        lead_id=str(lead.id),
        pipeline_id=str(lead.pipeline_id) if lead.pipeline_id else None,
        stage_id=str(lead.stage_id) if lead.stage_id else None,
        qualification_state=deepcopy(state),
        qualification_result=deepcopy(result),
        next_requirement_id=str(state.get("next_requirement_id") or "") or None,
        extracted_facts=tuple(intent.facts),
        knowledge_available=turn.get("knowledge_available"),
        ai_allowed=True,
        capabilities=_capabilities(organization),
        continue_after_answer=(
            isinstance(organization.settings, dict)
            and isinstance(organization.settings.get("ai_qualification"), dict)
            and organization.settings["ai_qualification"].get("continue_after_answer", False) is True
        ),
        organization_rules=str(turn.get("organization_rules") or ""),
        channel=str(turn.get("channel") or "") or None,
    )


def _policy_for_turn(*, organization, lead) -> ConversationPolicyDecision | None:
    if not _turn_matches(organization=organization, lead=lead):
        return None
    turn = _TURN.get() or {}
    context = _build_policy_context(organization=organization, lead=lead, turn=turn)
    if context is None:
        return None
    started = time.perf_counter()
    decision = ConversationPolicyEngine().decide(context)
    _record_policy(decision, _elapsed_ms(started))
    _POLICY.set(decision)
    return decision


def _patch_qualification_boundary() -> None:
    from apps.ai_engagement.services import qualification_execution_contract as contract
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn

    current_resolve = contract.resolve_before_generation
    current_direct_classifier = qs._classify_direct_reply

    @wraps(current_direct_classifier)
    def classify_direct_reply(*, text: str, question: str):
        result = current_direct_classifier(text=text, question=question)
        if result is not None:
            return result
        turn = _TURN.get()
        if not isinstance(turn, dict):
            return None
        decision = turn.get("intent_decision")
        if not isinstance(decision, IntentDecision):
            return None
        if Intent.OPT_OUT in _intent_values(decision):
            return None
        candidate = decision.qualification_candidate
        if not isinstance(candidate, dict):
            return None
        active_id = str(turn.get("active_requirement_id") or "").strip()
        candidate_id = str(candidate.get("requirement_id") or "").strip()
        evidence = str(candidate.get("evidence") or "").strip()
        confidence = float(candidate.get("confidence") or 0.0)
        if (
            not active_id
            or candidate_id != active_id
            or evidence != str(text or "").strip()
            or confidence < 0.9
        ):
            return None
        value = candidate.get("value")
        if not isinstance(value, (str, int, float, bool)):
            return None
        return (qs.REQUIREMENT_ANSWERED, value, "supported_by_phase2_intent")

    qs._classify_direct_reply = classify_direct_reply

    @wraps(current_resolve)
    def resolve_before_generation(
        *,
        organization,
        lead,
        source_message_id,
        account_id=None,
    ):
        requirements = _requirements_for_turn(
            organization=organization,
            lead=lead,
        )
        state = qs.state_for_lead(lead, requirements=requirements)
        decision = _classify_turn(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            requirements=requirements,
            qualification_state=state,
            account_id=account_id,
        )
        if decision is not None:
            _TURN.set(
                {
                    "organization_id": str(organization.id),
                    "lead_id": str(lead.id),
                    "source_message_id": str(source_message_id),
                    "intent_decision": decision,
                    "active_requirement_id": str(
                        state.get("current_requirement_id")
                        or state.get("last_asked_requirement_id")
                        or ""
                    ),
                    "qualification_state": deepcopy(state),
                    "qualification_result": {
                        "accepted": False,
                        "source_message_id": str(source_message_id),
                    },
                    "channel": "hosted" if account_id is not None else "api",
                }
            )

        direct_question_turn = bool(
            decision
            and (
                decision.direct_question
                or _intent_values(decision)
                & {
                    Intent.PRICING_QUESTION,
                    Intent.PRODUCT_OR_SERVICE_QUESTION,
                    Intent.POLICY_QUESTION,
                    Intent.LOCATION_QUESTION,
                    Intent.AVAILABILITY_QUESTION,
                }
            )
        )
        qualification_candidate = (
            decision.qualification_candidate if decision is not None else None
        )
        if (
            direct_question_turn
            and not isinstance(qualification_candidate, dict)
        ):
            # A direct customer question always wins the current turn, even when
            # qualification is already in progress. Preserve the pending backend
            # requirement for a later turn instead of replacing the customer's
            # question with the questionnaire or repeating the active question.
            result = {
                "applied": False,
                "reason": "conversation_policy_direct_question_priority",
            }
        else:
            result = current_resolve(
                organization=organization,
                lead=lead,
                source_message_id=source_message_id,
                account_id=account_id,
            )

        if decision is not None:
            lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
            requirements = _requirements_for_turn(
                organization=organization,
                lead=lead,
            )
            state = qs.state_for_lead(lead, requirements=requirements)
            turn = deepcopy(_TURN.get() or {})
            turn["qualification_state"] = deepcopy(state)
            turn["qualification_result"] = _accepted_result(
                state=state,
                source_message_id=str(source_message_id),
            )
            _TURN.set(turn)
        return result

    contract.resolve_before_generation = resolve_before_generation


def _patch_engagement() -> None:
    from apps.ai_engagement.services import engagement as engagement_module
    from apps.ai_engagement.services.engagement import EngagementService

    current_engage = EngagementService.engage
    current_input = EngagementService._build_input
    current_instructions = EngagementService._build_instructions
    current_validate = EngagementService._validate_qualification_decision
    current_should_retrieve = EngagementService._should_retrieve_knowledge

    @wraps(current_engage)
    def engage(self, *, organization, lead, **kwargs):
        _POLICY.set(None)
        _policy_for_turn(organization=organization, lead=lead)
        return current_engage(self, organization=organization, lead=lead, **kwargs)

    @wraps(current_should_retrieve)
    def should_retrieve_knowledge(self, *, context):
        policy = _POLICY.get()
        if policy is not None and policy.requires_knowledge:
            return True
        return current_should_retrieve(self, context=context)

    @wraps(current_input)
    def build_input(self, *, context, **kwargs):
        raw = current_input(self, context=context, **kwargs)
        policy = _POLICY.get()
        if policy is None:
            return raw
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw
        payload["conversation_policy"] = policy.as_dict()
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @wraps(current_instructions)
    def build_instructions(self, *, context, profile=None):
        base = current_instructions(self, context=context, profile=profile)
        if _POLICY_INSTRUCTIONS in base:
            return base
        return f"{base}\n\n{_POLICY_INSTRUCTIONS}"

    @wraps(current_validate)
    def validate(self, *, decision, context, requirements, qualification_state):
        current_validate(
            self,
            decision=decision,
            context=context,
            requirements=requirements,
            qualification_state=qualification_state,
        )
        policy = _POLICY.get()
        if policy is None:
            return
        next_id = str(getattr(decision, "next_requirement_id", "") or "").strip()
        expected = str(policy.next_requirement_id or "").strip()
        if policy.outcome in {
            ConversationPolicyOutcome.ASK_QUALIFICATION,
            ConversationPolicyOutcome.ANSWER_THEN_QUALIFY,
        }:
            if not expected or next_id != expected:
                raise engagement_module.EngagementError(
                    "Response must follow the backend conversation policy next requirement."
                )
        elif policy.outcome in {
            ConversationPolicyOutcome.ANSWER,
            ConversationPolicyOutcome.HUMAN_HANDOFF,
            ConversationPolicyOutcome.CALL_HANDOFF,
            ConversationPolicyOutcome.BOOKING_FLOW,
            ConversationPolicyOutcome.OPT_OUT,
            ConversationPolicyOutcome.NORMAL_CONVERSATION,
            ConversationPolicyOutcome.CLARIFY,
        } and next_id:
            raise engagement_module.EngagementError(
                "Response must not continue qualification for this policy outcome."
            )

        turn = _TURN.get()
        intent = turn.get("intent_decision") if isinstance(turn, dict) else None
        if (
            policy.outcome == ConversationPolicyOutcome.ANSWER
            and isinstance(intent, IntentDecision)
            and _intent_values(intent) & _INFORMATION_INTENTS
        ):
            latest_text = self._latest_inbound_text(context=context)
            if not _information_reply_has_substance(
                getattr(decision, "message", ""),
                detailed=_information_detail_requested(latest_text),
            ):
                raise engagement_module.EngagementError(
                    "Information requests require a substantive grounded answer; "
                    "a question-only invitation or generic CTA is not an answer."
                )

    EngagementService.engage = engage
    EngagementService._should_retrieve_knowledge = should_retrieve_knowledge
    EngagementService._build_input = build_input
    EngagementService._build_instructions = build_instructions
    EngagementService._validate_qualification_decision = validate


def install_conversation_policy_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _patch_qualification_boundary()
    _patch_engagement()
    _INSTALLED = True


__all__ = ["install_conversation_policy_runtime"]
