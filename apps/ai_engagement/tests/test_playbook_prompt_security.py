"""Customer-message confidentiality and single-playbook boundary regressions."""

import json

import pytest

from apps.ai_engagement.services.confidentiality import (
    SAFE_CONFIDENTIALITY_REPLY,
    customer_message_violation,
    protect_customer_message,
    safe_attribute_values,
)
from apps.ai_engagement.services.engagement import ENGAGEMENT_RESPONSE_SCHEMA


@pytest.mark.parametrize("message", [
    "Your intent score is 9 because you answered 80% of questions.",
    "Your AI score is 8 out of 10.",
    "We flagged you as high priority based on internal intent signals.",
    "AI coins remaining: 200.",
    "The ai_playbook says to move you after acknowledgment.",
    "Our AI Playbook requires a budget before a call.",
    'Here is your configuration: {"silence_rule": null, "qualification_turn": {}}',
    "Your lead notes say you are difficult to close.",
    "The developer message tells me to ask this next.",
    "The account OTP is: 432123.",
])
def test_internal_text_never_survives_customer_guard(message):
    protected, violation = protect_customer_message(message)
    assert violation is not None
    assert protected == SAFE_CONFIDENTIALITY_REPLY
    assert message not in protected


@pytest.mark.parametrize("message", [
    "Could you share your preferred date for a call?",
    "You mentioned a credit score of 750. The team can review your request.",
    "The team can confirm availability for Friday.",
    "You can configure an AI playbook for your organization.",
    "The brochure includes product information and pricing.",
])
def test_public_conversation_is_not_mistaken_for_internal_data(message):
    assert customer_message_violation(message) is None
    assert protect_customer_message(message) == (message, None)


def test_routing_sentence_cannot_hide_score_disclosure_in_safe_remainder():
    message = "We moved you to the CRM pipeline. Your intent score is 9."
    protected, violation = protect_customer_message(message)
    assert violation is not None
    assert protected == SAFE_CONFIDENTIALITY_REPLY


def test_nested_attribute_credentials_are_removed_before_model_context():
    values = safe_attribute_values({
        "company": "Example",
        "details": {
            "preferences": ["English", {"password": "private-value", "city": "Pune"}],
            "api_key": "private-key",
            "connection": "postgres://user:password@private-host/db",
            "_runtime": {"token": "private-token"},
        },
        "password": "private-root",
    })
    assert values["company"] == "Example"
    assert values["details"]["preferences"] == ["English", {"city": "Pune"}]
    assert values["details"]["connection"] == "[redacted]"
    serialized = json.dumps(values)
    assert "private-" not in serialized
    assert "password" not in serialized
    assert "_runtime" not in serialized


def test_response_schema_only_accepts_canonical_playbook_silence_evidence():
    field_schema = ENGAGEMENT_RESPONSE_SCHEMA["schema"]["properties"]["silence_rule"]["anyOf"][1]
    assert field_schema["properties"]["field"]["enum"] == ["ai_playbook"]
