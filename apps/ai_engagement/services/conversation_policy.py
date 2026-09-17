from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping

from apps.ai_engagement.services.intent_types import Intent, IntentDecision


class ConversationPolicyOutcome(StrEnum):
    ANSWER = "ANSWER"
    ASK_QUALIFICATION = "ASK_QUALIFICATION"
    ANSWER_THEN_QUALIFY = "ANSWER_THEN_QUALIFY"
    CLARIFY = "CLARIFY"
    BOOKING_FLOW = "BOOKING_FLOW"
    CALL_HANDOFF = "CALL_HANDOFF"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    OPT_OUT = "OPT_OUT"
    NORMAL_CONVERSATION = "NORMAL_CONVERSATION"
    WAIT = "WAIT"
    NO_ACTION = "NO_ACTION"


@dataclass(frozen=True)
class ConversationPolicyContext:
    intent_decision: IntentDecision
    organization_id: str
    lead_id: str
    pipeline_id: str | None = None
    stage_id: str | None = None
    qualification_state: Mapping[str, Any] | None = None
    qualification_result: Mapping[str, Any] | None = None
    next_requirement_id: str | None = None
    extracted_facts: tuple[Mapping[str, Any], ...] = ()
    knowledge_available: bool | None = None
    ai_allowed: bool = True
    capabilities: frozenset[str] = frozenset()
    organization_rules: str = ""
    channel: str | None = None


@dataclass(frozen=True)
class ConversationPolicyDecision:
    outcome: ConversationPolicyOutcome
    reason_code: str
    confidence: float
    answer_customer_question: bool = False
    continue_qualification: bool = False
    next_requirement_id: str | None = None
    requires_knowledge: bool = False
    requires_human: bool = False
    handoff_type: str | None = None
    booking_requested: bool = False
    should_respond: bool = True
    allowed_response_goal: str = "normal_conversation"
    qualification_result_reference: str | None = None
    policy_source: str = "deterministic_python"
    explanation_debug_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        return payload


_QUESTION_INTENTS = {
    Intent.PRICING_QUESTION,
    Intent.PRODUCT_OR_SERVICE_QUESTION,
    Intent.POLICY_QUESTION,
    Intent.LOCATION_QUESTION,
    Intent.AVAILABILITY_QUESTION,
}


class ConversationPolicyEngine:
    """Pure backend conversation strategy; never performs CRM/database mutations."""

    def decide(self, context: ConversationPolicyContext) -> ConversationPolicyDecision:
        intent = context.intent_decision
        intents = {intent.primary_intent, *intent.secondary_intents}
        next_requirement_id = str(context.next_requirement_id or "").strip() or None
        accepted = self._qualification_accepted(context)
        result_ref = self._qualification_result_reference(context)
        confidence = max(0.0, min(float(intent.confidence or 0.0), 1.0))
        asks_question = bool(intent.direct_question) or bool(intents & _QUESTION_INTENTS)

        if not context.ai_allowed:
            return self._decision(
                ConversationPolicyOutcome.NO_ACTION,
                "BACKEND_HARD_STOP",
                confidence,
                should_respond=False,
                goal="no_action",
                debug="Backend permission/eligibility denied this AI turn.",
            )

        if Intent.OPT_OUT in intents:
            return self._decision(
                ConversationPolicyOutcome.OPT_OUT,
                "OPT_OUT_PRECEDENCE",
                confidence,
                should_respond=False,
                goal="opt_out",
                debug="Explicit opt-out takes precedence over all secondary intents.",
            )

        if Intent.HUMAN_REQUEST in intents:
            return self._decision(
                ConversationPolicyOutcome.HUMAN_HANDOFF,
                "HUMAN_REQUEST",
                confidence,
                requires_human=True,
                handoff_type="human",
                goal="human_handoff",
                debug="Customer explicitly requested a human.",
            )

        if Intent.CALL_REQUEST in intents:
            call_supported = "call" in context.capabilities
            handoff_type = "call" if call_supported else "human"
            reason_prefix = "CALL_REQUEST_SUPPORTED" if call_supported else "CALL_REQUEST_SAFE_FALLBACK"

            # A call request is an action requirement, not permission to discard
            # another explicit customer question or a qualification answer in the
            # same message. Preserve those conversational obligations while the
            # backend separately validates/proposes the call/reminder action.
            if asks_question and accepted and next_requirement_id:
                return self._decision(
                    ConversationPolicyOutcome.ANSWER_THEN_QUALIFY,
                    f"{reason_prefix}_ANSWER_THEN_QUALIFY",
                    confidence,
                    answer=True,
                    continue_qualification=True,
                    next_requirement_id=next_requirement_id,
                    requires_knowledge=bool(intent.requires_knowledge),
                    requires_human=True,
                    handoff_type=handoff_type,
                    goal=(
                        "answer_customer_then_ask_next_qualification_and_arrange_call"
                        if call_supported
                        else "answer_customer_then_ask_next_qualification_and_human_handoff_without_call_confirmation"
                    ),
                    result_ref=result_ref,
                    debug=(
                        "Preserve the direct question and accepted qualification answer; "
                        "the validated backend call capability is handled in parallel."
                    ),
                )

            if asks_question:
                return self._decision(
                    ConversationPolicyOutcome.ANSWER,
                    f"{reason_prefix}_ANSWER",
                    confidence,
                    answer=True,
                    requires_knowledge=bool(intent.requires_knowledge),
                    requires_human=True,
                    handoff_type=handoff_type,
                    goal=(
                        "answer_customer_and_arrange_call"
                        if call_supported
                        else "answer_customer_and_human_handoff_without_call_confirmation"
                    ),
                    result_ref=result_ref,
                    debug=(
                        "Answer the direct customer question while preserving the call "
                        "request as a backend action/handoff requirement."
                    ),
                )

            if accepted and next_requirement_id:
                return self._decision(
                    ConversationPolicyOutcome.ASK_QUALIFICATION,
                    f"{reason_prefix}_CONTINUE_QUALIFICATION",
                    confidence,
                    continue_qualification=True,
                    next_requirement_id=next_requirement_id,
                    requires_human=True,
                    handoff_type=handoff_type,
                    goal=(
                        "ask_next_qualification_and_arrange_call"
                        if call_supported
                        else "ask_next_qualification_and_human_handoff_without_call_confirmation"
                    ),
                    result_ref=result_ref,
                    debug=(
                        "The current qualification answer was accepted; continue with the "
                        "backend-selected next requirement while preserving the call request."
                    ),
                )

            if call_supported:
                return self._decision(
                    ConversationPolicyOutcome.CALL_HANDOFF,
                    "CALL_REQUEST_SUPPORTED",
                    confidence,
                    requires_human=True,
                    handoff_type="call",
                    goal="call_handoff",
                    debug="Call request is supported by current backend capabilities.",
                )
            return self._decision(
                ConversationPolicyOutcome.HUMAN_HANDOFF,
                "CALL_REQUEST_SAFE_FALLBACK",
                confidence,
                requires_human=True,
                handoff_type="human",
                goal="human_handoff_without_call_confirmation",
                debug="No validated call capability exists; do not fabricate a call.",
            )

        if Intent.BOOKING_INTENT in intents:
            if "booking" in context.capabilities:
                return self._decision(
                    ConversationPolicyOutcome.BOOKING_FLOW,
                    "BOOKING_SUPPORTED",
                    confidence,
                    booking_requested=True,
                    goal="booking_flow",
                    debug="Booking intent is supported by a validated backend capability.",
                )
            return self._decision(
                ConversationPolicyOutcome.HUMAN_HANDOFF,
                "BOOKING_SAFE_FALLBACK",
                confidence,
                requires_human=True,
                handoff_type="human",
                booking_requested=True,
                goal="human_handoff_without_booking_confirmation",
                debug="No validated booking capability exists; do not invent a booking.",
            )

        if asks_question:
            if accepted and next_requirement_id:
                return self._decision(
                    ConversationPolicyOutcome.ANSWER_THEN_QUALIFY,
                    "DIRECT_QUESTION_AFTER_ACCEPTED_QUALIFICATION",
                    confidence,
                    answer=True,
                    continue_qualification=True,
                    next_requirement_id=next_requirement_id,
                    requires_knowledge=bool(intent.requires_knowledge),
                    goal="answer_customer_then_ask_next_qualification",
                    result_ref=result_ref,
                    debug=(
                        "Answer the direct question first, then ask only the backend "
                        "selected next unanswered requirement."
                    ),
                )
            return self._decision(
                ConversationPolicyOutcome.ANSWER,
                "DIRECT_CUSTOMER_QUESTION",
                confidence,
                answer=True,
                requires_knowledge=bool(intent.requires_knowledge),
                goal="answer_customer_question",
                result_ref=result_ref,
                debug="Direct customer question takes priority over qualification continuation.",
            )

        if accepted:
            if next_requirement_id:
                return self._decision(
                    ConversationPolicyOutcome.ASK_QUALIFICATION,
                    "ACCEPTED_QUALIFICATION_CONTINUE",
                    confidence,
                    continue_qualification=True,
                    next_requirement_id=next_requirement_id,
                    goal="ask_next_qualification",
                    result_ref=result_ref,
                    debug="Current answer was accepted; continue with next unanswered requirement.",
                )
            return self._decision(
                ConversationPolicyOutcome.NORMAL_CONVERSATION,
                "QUALIFICATION_COMPLETE",
                confidence,
                goal="normal_conversation_after_qualification",
                result_ref=result_ref,
                debug="Current qualification turn completed the configured requirements.",
            )

        incomplete = bool(next_requirement_id)
        if Intent.GREETING in intents and incomplete:
            return self._decision(
                ConversationPolicyOutcome.ASK_QUALIFICATION,
                "GREETING_CONTINUE_QUALIFICATION",
                confidence,
                continue_qualification=True,
                next_requirement_id=next_requirement_id,
                goal="greet_then_ask_next_qualification",
                debug="Greeting is compatible with backend-owned qualification continuation.",
            )

        if intent.primary_intent in {Intent.AMBIGUOUS, Intent.UNKNOWN}:
            return self._decision(
                ConversationPolicyOutcome.CLARIFY,
                "AMBIGUOUS_OR_UNKNOWN",
                confidence,
                goal="clarify_customer_intent",
                debug="Intent is not clear enough for a stronger workflow decision.",
            )

        return self._decision(
            ConversationPolicyOutcome.NORMAL_CONVERSATION,
            "NORMAL_CONVERSATION",
            confidence,
            requires_knowledge=bool(intent.requires_knowledge),
            goal="normal_conversation",
            debug="No higher-precedence deterministic policy rule matched.",
        )

    @staticmethod
    def _qualification_accepted(context: ConversationPolicyContext) -> bool:
        result = context.qualification_result or {}
        if result.get("accepted") is True:
            return True
        source_id = str(result.get("source_message_id") or "").strip()
        if not source_id:
            return False
        state = context.qualification_state or {}
        for item in (state.get("requirement_states") or {}).values():
            if not isinstance(item, Mapping):
                continue
            if (
                str(item.get("source_message_id") or "") == source_id
                and str(item.get("status") or "").casefold() == "answered"
            ):
                return True
        return False

    @staticmethod
    def _qualification_result_reference(context: ConversationPolicyContext) -> str | None:
        value = str((context.qualification_result or {}).get("source_message_id") or "")
        return value.strip() or None

    @staticmethod
    def _decision(
        outcome: ConversationPolicyOutcome,
        reason_code: str,
        confidence: float,
        *,
        answer: bool = False,
        continue_qualification: bool = False,
        next_requirement_id: str | None = None,
        requires_knowledge: bool = False,
        requires_human: bool = False,
        handoff_type: str | None = None,
        booking_requested: bool = False,
        should_respond: bool = True,
        goal: str,
        result_ref: str | None = None,
        debug: str,
    ) -> ConversationPolicyDecision:
        return ConversationPolicyDecision(
            outcome=outcome,
            reason_code=reason_code,
            confidence=confidence,
            answer_customer_question=answer,
            continue_qualification=continue_qualification,
            next_requirement_id=next_requirement_id,
            requires_knowledge=requires_knowledge,
            requires_human=requires_human,
            handoff_type=handoff_type,
            booking_requested=booking_requested,
            should_respond=should_respond,
            allowed_response_goal=goal,
            qualification_result_reference=result_ref,
            explanation_debug_reason=debug,
        )


__all__ = [
    "ConversationPolicyContext",
    "ConversationPolicyDecision",
    "ConversationPolicyEngine",
    "ConversationPolicyOutcome",
]
