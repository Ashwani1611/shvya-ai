from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from apps.ai_engagement.services.conversation_policy import (
    ConversationPolicyContext,
    ConversationPolicyEngine,
    ConversationPolicyOutcome,
)
from apps.ai_engagement.services.conversation_policy_runtime import (
    _affirmed_information_offer,
    _record_policy,
)
from apps.ai_engagement.services.intent_types import (
    ClassificationPath,
    Intent,
    IntentDecision,
)


def intent(
    primary: Intent,
    *secondary: Intent,
    direct_question: str | None = None,
    facts=(),
    requires_knowledge: bool = False,
) -> IntentDecision:
    return IntentDecision(
        primary_intent=primary,
        secondary_intents=tuple(secondary),
        confidence=0.97,
        facts=tuple(facts),
        direct_question=direct_question,
        classification_path=ClassificationPath.DETERMINISTIC,
        requires_knowledge=requires_knowledge,
    )


def context(
    decision: IntentDecision,
    *,
    accepted: bool = False,
    next_id: str | None = None,
    capabilities=(),
    channel: str = "api",
    state=None,
) -> ConversationPolicyContext:
    source_id = "m1"
    qualification_state = state or {
        "requirement_states": {},
        "next_requirement_id": next_id,
    }
    return ConversationPolicyContext(
        intent_decision=decision,
        organization_id="org-a",
        lead_id="lead-a",
        pipeline_id="pipe-a",
        stage_id="stage-a",
        qualification_state=qualification_state,
        qualification_result={
            "accepted": accepted,
            "source_message_id": source_id,
        },
        next_requirement_id=next_id,
        extracted_facts=tuple(decision.facts),
        capabilities=frozenset(capabilities),
        channel=channel,
    )


def decide(ctx: ConversationPolicyContext):
    return ConversationPolicyEngine().decide(ctx)


def test_yes_after_feature_offer_is_treated_as_conversation_continuation():
    manager = Mock()
    query = Mock()
    manager.filter.return_value = query
    query.filter.return_value = query
    query.order_by.return_value = query
    query.only.return_value = query
    query.first.return_value = SimpleNamespace(
        body="Would you like details on our plans or specific features?"
    )
    lead = SimpleNamespace(organization_id="org-a", whatsapp_messages=manager)
    source = SimpleNamespace(body="yes", created_at=object(), account_id="account-a")

    assert _affirmed_information_offer(lead=lead, source=source) is True

    query.first.return_value = SimpleNamespace(
        body="What is your biggest challenge with managing leads?"
    )
    assert _affirmed_information_offer(lead=lead, source=source) is False


def test_qualification_answer_only_continues_to_next_requirement():
    result = decide(context(intent(Intent.QUALIFICATION_ANSWER), accepted=True, next_id="q4"))
    assert result.outcome == ConversationPolicyOutcome.ASK_QUALIFICATION
    assert result.next_requirement_id == "q4"


def test_pricing_only_answers_without_forcing_qualification():
    result = decide(
        context(
            intent(
                Intent.PRICING_QUESTION,
                direct_question="What is your price?",
                requires_knowledge=True,
            ),
            next_id="q3",
        )
    )
    assert result.outcome == ConversationPolicyOutcome.ANSWER
    assert result.continue_qualification is False


def test_qualification_plus_pricing_answers_without_forced_next_question():
    result = decide(
        context(
            intent(
                Intent.PRICING_QUESTION,
                Intent.QUALIFICATION_ANSWER,
                direct_question="What is your price?",
                requires_knowledge=True,
            ),
            accepted=True,
            next_id="q4",
        )
    )
    assert result.outcome == ConversationPolicyOutcome.ANSWER
    assert result.answer_customer_question is True
    assert result.continue_qualification is False
    assert result.next_requirement_id is None


def test_qualification_plus_product_question_answers_without_forced_next_question():
    result = decide(
        context(
            intent(
                Intent.PRODUCT_OR_SERVICE_QUESTION,
                Intent.QUALIFICATION_ANSWER,
                direct_question="What does your product do?",
            ),
            accepted=True,
            next_id="q4",
        )
    )
    assert result.outcome == ConversationPolicyOutcome.ANSWER
    assert result.continue_qualification is False
    assert result.next_requirement_id is None


def test_multiple_qualification_facts_preserve_backend_requirement_for_later():
    facts = (
        {"requirement_id": "tool", "value": "Excel"},
        {"requirement_id": "volume", "value": 25},
    )
    result = decide(
        context(
            intent(
                Intent.PRICING_QUESTION,
                Intent.QUALIFICATION_ANSWER,
                direct_question="What are your charges?",
                facts=facts,
            ),
            accepted=True,
            next_id="ads",
        )
    )
    assert result.outcome == ConversationPolicyOutcome.ANSWER
    assert result.continue_qualification is False
    assert result.next_requirement_id is None


def test_human_request_has_handoff_precedence():
    result = decide(context(intent(Intent.HUMAN_REQUEST)))
    assert result.outcome == ConversationPolicyOutcome.HUMAN_HANDOFF
    assert result.requires_human is True


def test_call_request_uses_supported_call_handoff():
    result = decide(context(intent(Intent.CALL_REQUEST), capabilities={"call"}))
    assert result.outcome == ConversationPolicyOutcome.CALL_HANDOFF


def test_call_unavailable_falls_back_without_fabricated_call():
    result = decide(context(intent(Intent.CALL_REQUEST)))
    assert result.outcome == ConversationPolicyOutcome.HUMAN_HANDOFF
    assert "without_call_confirmation" in result.allowed_response_goal


def test_booking_supported_enters_booking_flow():
    result = decide(context(intent(Intent.BOOKING_INTENT), capabilities={"booking"}))
    assert result.outcome == ConversationPolicyOutcome.BOOKING_FLOW


def test_booking_unavailable_falls_back_without_confirmation():
    result = decide(context(intent(Intent.BOOKING_INTENT)))
    assert result.outcome == ConversationPolicyOutcome.HUMAN_HANDOFF
    assert result.booking_requested is True
    assert "without_booking_confirmation" in result.allowed_response_goal


def test_opt_out_has_highest_customer_intent_precedence():
    result = decide(
        context(
            intent(
                Intent.OPT_OUT,
                Intent.PRICING_QUESTION,
                Intent.QUALIFICATION_ANSWER,
                direct_question="What is the price?",
            ),
            accepted=True,
            next_id="q4",
        )
    )
    assert result.outcome == ConversationPolicyOutcome.OPT_OUT
    assert result.continue_qualification is False
    assert result.should_respond is False


def test_backend_hard_stop_precedes_all_customer_intents():
    ctx = context(intent(Intent.PRICING_QUESTION, direct_question="Price?"))
    ctx = ConversationPolicyContext(**{**ctx.__dict__, "ai_allowed": False})
    result = decide(ctx)
    assert result.outcome == ConversationPolicyOutcome.NO_ACTION
    assert result.should_respond is False


def test_ambiguous_message_clarifies():
    result = decide(context(intent(Intent.AMBIGUOUS)))
    assert result.outcome == ConversationPolicyOutcome.CLARIFY


def test_greeting_with_incomplete_qualification_continues():
    result = decide(context(intent(Intent.GREETING), next_id="q2"))
    assert result.outcome == ConversationPolicyOutcome.ASK_QUALIFICATION
    assert result.next_requirement_id == "q2"


def test_previously_answered_requirement_is_never_selected_by_policy():
    state = {
        "requirement_states": {
            "q1": {"status": "answered"},
            "q2": {"status": "answered", "source_message_id": "m1"},
            "q3": {"status": "unknown"},
        },
        "next_requirement_id": "q3",
    }
    result = decide(
        context(
            intent(Intent.QUALIFICATION_ANSWER),
            accepted=True,
            next_id="q3",
            state=state,
        )
    )
    assert result.next_requirement_id == "q3"
    assert result.next_requirement_id not in {"q1", "q2"}


def test_qualification_completion_does_not_invent_next_question():
    result = decide(context(intent(Intent.QUALIFICATION_ANSWER), accepted=True))
    assert result.outcome == ConversationPolicyOutcome.NORMAL_CONVERSATION
    assert result.next_requirement_id is None


def test_api_and_hosted_produce_equivalent_policy_outcomes():
    decision = intent(
        Intent.PRICING_QUESTION,
        Intent.QUALIFICATION_ANSWER,
        direct_question="Price?",
    )
    api = decide(context(decision, accepted=True, next_id="q4", channel="api"))
    hosted = decide(context(decision, accepted=True, next_id="q4", channel="hosted"))
    assert api.as_dict() == hosted.as_dict()


def test_coexistence_uses_same_policy_engine():
    decision = intent(Intent.GREETING)
    api = decide(context(decision, next_id="q1", channel="api"))
    coexistence = decide(context(decision, next_id="q1", channel="coexistence"))
    assert api.as_dict() == coexistence.as_dict()


def test_policy_does_not_mutate_qualification_state_or_facts():
    state = {
        "requirement_states": {"q1": {"status": "answered", "value": "A"}},
        "next_requirement_id": "q2",
    }
    facts = ({"requirement_id": "q1", "value": "A"},)
    ctx = context(
        intent(Intent.QUALIFICATION_ANSWER, facts=facts),
        accepted=True,
        next_id="q2",
        state=state,
    )
    before_state = copy.deepcopy(state)
    before_facts = copy.deepcopy(facts)
    decide(ctx)
    assert state == before_state
    assert facts == before_facts


def test_policy_tenant_inputs_are_explicit_and_compact():
    ctx_a = context(intent(Intent.THANK_YOU))
    ctx_b = ConversationPolicyContext(**{**ctx_a.__dict__, "organization_id": "org-b"})
    assert ctx_a.organization_id == "org-a"
    assert ctx_b.organization_id == "org-b"
    assert not hasattr(ctx_a, "organization")


def test_policy_has_no_model_provider_dependency_for_deterministic_cases():
    with patch(
        "apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text"
    ) as generate:
        result = decide(context(intent(Intent.CALL_REQUEST), capabilities={"call"}))
    assert result.outcome == ConversationPolicyOutcome.CALL_HANDOFF
    generate.assert_not_called()


def test_policy_trace_records_outcome_and_latency():
    policy = decide(context(intent(Intent.PRICING_QUESTION, direct_question="Price?")))
    with patch("apps.ai_engagement.services.trace_service.record") as record:
        _record_policy(policy, 3)
    assert record.call_args_list[0].args[0] == "policy"
    assert record.call_args_list[0].args[1]["outcome"] == "ANSWER"
    assert record.call_args_list[1].args == ("performance", {"policy_ms": 3})


def test_trace_failure_does_not_break_policy():
    policy = decide(context(intent(Intent.THANK_YOU)))
    with patch(
        "apps.ai_engagement.services.trace_service.record",
        side_effect=RuntimeError("trace down"),
    ):
        _record_policy(policy, 1)


def test_thank_you_is_normal_conversation():
    result = decide(context(intent(Intent.THANK_YOU)))
    assert result.outcome == ConversationPolicyOutcome.NORMAL_CONVERSATION


def test_complaint_without_configured_handoff_remains_normal_conversation():
    result = decide(context(intent(Intent.COMPLAINT)))
    assert result.outcome == ConversationPolicyOutcome.NORMAL_CONVERSATION


def test_acceptance_scenario_around_30_plus_pricing():
    decision = intent(
        Intent.PRICING_QUESTION,
        Intent.QUALIFICATION_ANSWER,
        direct_question="Also what is your pricing?",
        facts=({"requirement_id": "volume", "value": 30},),
        requires_knowledge=True,
    )
    state = {
        "requirement_states": {
            "volume": {
                "status": "answered",
                "value": 30,
                "source_message_id": "m1",
            },
            "ads": {"status": "unknown"},
        },
        "next_requirement_id": "ads",
    }
    result = decide(
        context(
            decision,
            accepted=True,
            next_id="ads",
            state=state,
        )
    )
    assert result.outcome == ConversationPolicyOutcome.ANSWER
    assert result.answer_customer_question is True
    assert result.continue_qualification is False
    assert result.next_requirement_id is None
    assert result.requires_knowledge is True


def test_answered_state_can_prove_acceptance_even_if_result_flag_is_false():
    state = {
        "requirement_states": {
            "volume": {"status": "answered", "source_message_id": "m1"},
            "ads": {"status": "unknown"},
        },
        "next_requirement_id": "ads",
    }
    result = decide(
        context(
            intent(Intent.QUALIFICATION_ANSWER),
            accepted=False,
            next_id="ads",
            state=state,
        )
    )
    assert result.outcome == ConversationPolicyOutcome.ASK_QUALIFICATION


def test_canonical_outcome_enum_is_bounded():
    assert {item.value for item in ConversationPolicyOutcome} == {
        "ANSWER",
        "ASK_QUALIFICATION",
        "ANSWER_THEN_QUALIFY",
        "CLARIFY",
        "BOOKING_FLOW",
        "CALL_HANDOFF",
        "HUMAN_HANDOFF",
        "OPT_OUT",
        "NORMAL_CONVERSATION",
        "WAIT",
        "NO_ACTION",
    }
