from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.ai_engagement.services.ai_provider import AIProviderError, AITextResult
from apps.ai_engagement.services.intent_engine import (
    ClassificationPath, Intent, IntentDecision, IntentEngine, IntentScopeError,
)
from apps.ai_engagement.services import intent_runtime


class FakeProvider:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"primary_intent": "AMBIGUOUS", "secondary_intents": [], "confidence": .4,
            "entities": [], "facts": [], "direct_question": None, "qualification_candidate": None,
            "requested_action": None, "language": "en", "requires_knowledge": False, "requires_human": False}
        self.error, self.calls = error, []
    def generate_text(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return AITextResult(text=json.dumps(self.payload), model="configured-test-model")


@pytest.fixture
def env():
    org = SimpleNamespace(id="org-a")
    lead = SimpleNamespace(id="lead-a", organization_id="org-a", attributes={"existing": "safe"})
    reqs = [
        {"id": "challenge", "label": "Biggest challenge", "question": "What is your biggest challenge?\nA. Slow replies\nB. Missed follow-ups\nC. Leads going cold\nD. No proper tracking"},
        {"id": "tool", "label": "Lead tool", "question": "Which tool do you use to manage leads?"},
        {"id": "volume", "label": "Daily lead volume", "question": "How many leads do you receive daily?"},
        {"id": "ads", "label": "Running ads", "question": "Are you running ads?"},
    ]
    return org, lead, reqs


def classify(env, text, *, active=None, provider=None, requirements=None, context=None):
    org, lead, reqs = env
    state = {"current_requirement_id": active, "last_asked_requirement_id": active} if active else {}
    return IntentEngine(provider=provider).classify(
        organization=org, lead=lead, message=text, source_message_id="m1",
        requirements=reqs if requirements is None else requirements,
        qualification_state=state, context=context,
    )


@pytest.mark.parametrize(("text", "expected"), [
    ("Hi", Intent.GREETING),
    ("I want to know more about your services.", Intent.PRODUCT_OR_SERVICE_QUESTION),
    ("What is shvya", Intent.PRODUCT_OR_SERVICE_QUESTION),
    ("What is shvya ai", Intent.PRODUCT_OR_SERVICE_QUESTION),
    ("I want to know about shvya", Intent.PRODUCT_OR_SERVICE_QUESTION),
    ("What is its functionality", Intent.PRODUCT_OR_SERVICE_QUESTION),
    ("What is your price?", Intent.PRICING_QUESTION),
    ("I want to speak with someone.", Intent.HUMAN_REQUEST),
    ("Please call me tomorrow.", Intent.CALL_REQUEST),
    ("This is too expensive.", Intent.OBJECTION),
    ("How can I get started?", Intent.BUYING_INTENT),
    ("I'm unhappy with the service.", Intent.COMPLAINT),
    ("Stop messaging me.", Intent.OPT_OUT),
    ("Thanks", Intent.THANK_YOU),
    ("price kya hai?", Intent.PRICING_QUESTION),
    ("mujhe kisi person se baat karni hai", Intent.HUMAN_REQUEST),
])
def test_canonical_deterministic_intents(env, text, expected):
    result = classify(env, text)
    assert result.primary_intent == expected
    assert result.classification_path == ClassificationPath.DETERMINISTIC


def test_direct_product_question_without_question_mark_is_not_qualification_answer(env):
    result = classify(env, "What is shvya", active="challenge")
    assert result.primary_intent == Intent.PRODUCT_OR_SERVICE_QUESTION
    assert result.direct_question == "What is shvya"
    assert result.qualification_candidate is None
    assert Intent.QUALIFICATION_ANSWER not in result.secondary_intents


def test_natural_product_request_is_not_consumed_by_active_qualification(env):
    result = classify(env, "I want to know about shvya", active="challenge")
    assert result.primary_intent == Intent.PRODUCT_OR_SERVICE_QUESTION
    assert result.qualification_candidate is None
    assert Intent.QUALIFICATION_ANSWER not in result.secondary_intents
    assert result.requires_knowledge is True


def test_specific_pricing_question_is_not_double_classified_as_product(env):
    result = classify(env, "What is your pricing?")
    assert result.primary_intent == Intent.PRICING_QUESTION
    assert Intent.PRODUCT_OR_SERVICE_QUESTION not in result.secondary_intents


def test_affirmative_ack_is_not_freeform_nonboolean_qualification(env):
    result = classify(env, "yes", active="challenge", provider=FakeProvider())
    assert result.qualification_candidate is None
    assert Intent.QUALIFICATION_ANSWER not in {result.primary_intent, *result.secondary_intents}


def test_numeric_qualification(env):
    result = classify(env, "30", active="volume")
    assert result.primary_intent == Intent.QUALIFICATION_ANSWER
    assert result.qualification_candidate["value"] == 30


def test_natural_numeric_qualification(env):
    result = classify(env, "Around 25 leads every day.", active="volume")
    assert result.qualification_candidate["value"] == 25


@pytest.mark.parametrize("text", ["some time", "sometimes", "occasionally", "from time to time"])
def test_intermittent_boolean_qualification_is_yes(env, text):
    result = classify(env, text, active="ads")
    assert result.primary_intent == Intent.QUALIFICATION_ANSWER
    assert result.qualification_candidate["value"] is True
    assert result.classification_path == ClassificationPath.DETERMINISTIC


def test_configured_option_letter_and_natural_text(env):
    assert classify(env, "B", active="challenge").qualification_candidate["value"] == "Missed follow-ups"
    assert classify(env, "We are mainly struggling with missed follow-ups.", active="challenge").qualification_candidate["value"] == "Missed follow-ups"


def test_multiple_qualification_facts(env):
    result = classify(env, "We use Excel and get around 25 leads every day.", active="tool")
    facts = {item["requirement_id"]: item["value"] for item in result.facts}
    assert facts["tool"] == "Excel" and facts["volume"] == 25


def test_multi_intent_qualification_pricing_and_primary(env):
    result = classify(env, "We receive 30 leads every day. What is your pricing?", active="volume")
    assert result.primary_intent == Intent.PRICING_QUESTION
    assert Intent.QUALIFICATION_ANSWER in result.secondary_intents
    assert result.qualification_candidate["value"] == 30
    assert result.direct_question == "What is your pricing?"


def test_multi_intent_qualification_product(env):
    result = classify(env, "About 30. What exactly does your product do?", active="volume")
    assert {Intent.QUALIFICATION_ANSWER, Intent.PRODUCT_OR_SERVICE_QUESTION} <= {result.primary_intent, *result.secondary_intents}


def test_hinglish_binary_qualification(env):
    result = classify(env, "haan hum ads chala rahe hain", active="ads")
    assert result.primary_intent == Intent.QUALIFICATION_ANSWER
    assert result.qualification_candidate["value"] is True
    assert result.language == "hinglish"


def test_ambiguous_uses_one_structured_existing_provider_call(env):
    provider = FakeProvider()
    result = classify(env, "Context dependent", provider=provider)
    assert result.primary_intent == Intent.AMBIGUOUS and len(provider.calls) == 1
    call = provider.calls[0]
    assert call["metadata"]["task"] == "engagement"
    assert call["metadata"]["phase"] == "intent_classification"
    assert call["response_schema"]["strict"] is True


def test_invalid_or_failed_model_fails_soft_with_one_call(env):
    bad = FakeProvider(payload={"not": "schema"})
    assert classify(env, "Context dependent", provider=bad).primary_intent == Intent.UNKNOWN
    assert len(bad.calls) == 1
    failed = FakeProvider(error=AIProviderError("down"))
    assert classify(env, "Context dependent", provider=failed).classification_path == ClassificationPath.FALLBACK
    assert len(failed.calls) == 1


def test_tenant_and_context_isolation(env):
    org, lead, _ = env
    wrong = SimpleNamespace(id="other", organization_id="org-b", attributes={})
    with pytest.raises(IntentScopeError):
        IntentEngine(provider=FakeProvider()).classify(organization=org, lead=wrong, message="Hi")
    context = SimpleNamespace(organization={"id": "org-b"}, lead={"id": str(lead.id)})
    with pytest.raises(IntentScopeError):
        classify(env, "Hi", context=context)


def test_org_a_config_never_influences_org_b():
    req = [{"id": "q", "question": "Choose one\nA. Alpha\nB. Secret Org A Value"}]
    a = (SimpleNamespace(id="a"), SimpleNamespace(id="la", organization_id="a", attributes={}), req)
    b = (SimpleNamespace(id="b"), SimpleNamespace(id="lb", organization_id="b", attributes={}), [])
    assert classify(a, "B", active="q").qualification_candidate["value"] == "Secret Org A Value"
    assert classify(b, "B", active="q", requirements=[]).primary_intent != Intent.QUALIFICATION_ANSWER


def test_api_and_hosted_equivalent(env):
    org, lead, _ = env
    values = []
    for transport in ("api", "hosted"):
        context = SimpleNamespace(organization={"id": str(org.id)}, lead={"id": str(lead.id)}, transport=transport)
        values.append(classify(env, "We receive around 30 leads daily. What is your price?", active="volume", context=context).as_dict())
    assert values[0] == values[1]


def test_engine_has_no_crm_side_effects(env):
    _, lead, _ = env
    state = {"current_requirement_id": "volume", "last_asked_requirement_id": "volume", "requirement_states": {"volume": {"status": "asked"}}}
    before_attributes, before_state = copy.deepcopy(lead.attributes), copy.deepcopy(state)
    IntentEngine(provider=FakeProvider()).classify(organization=env[0], lead=lead, message="30", requirements=env[2], qualification_state=state)
    assert lead.attributes == before_attributes and state == before_state


def test_trace_records_intent_and_timing_fail_soft():
    decision = IntentDecision(primary_intent=Intent.PRICING_QUESTION, secondary_intents=(Intent.QUALIFICATION_ANSWER,),
        confidence=.97, facts=({"key": "volume", "value": 30, "evidence": "30 leads"},),
        classification_path=ClassificationPath.DETERMINISTIC)
    with patch("apps.ai_engagement.services.trace_service.record") as record:
        intent_runtime._record_trace(decision=decision, elapsed_ms=7)
    assert record.call_args_list[0].args[0] == "intent"
    assert record.call_args_list[0].args[1]["primary_intent"] == "PRICING_QUESTION"
    assert record.call_args_list[1].args == ("performance", {"intent_ms": 7})
    with patch("apps.ai_engagement.services.trace_service.record", side_effect=RuntimeError("trace down")):
        intent_runtime._record_trace(decision=decision, elapsed_ms=2)


@pytest.mark.parametrize(("text", "active"), [("Hi", None), ("Stop", None), ("30", "volume"), ("B", "challenge")])
def test_obvious_messages_make_zero_intent_model_calls(env, text, active):
    provider = FakeProvider()
    result = classify(env, text, active=active, provider=provider)
    assert result.classification_path == ClassificationPath.DETERMINISTIC
    assert provider.calls == []


def test_acceptance_scenario(env):
    result = classify(env, "We receive around 30 leads daily. What is your price?", active="volume")
    assert result.primary_intent == Intent.PRICING_QUESTION
    assert Intent.QUALIFICATION_ANSWER in result.secondary_intents
    assert result.qualification_candidate["value"] == 30
    assert result.direct_question == "What is your price?"
    assert result.requires_knowledge is True
