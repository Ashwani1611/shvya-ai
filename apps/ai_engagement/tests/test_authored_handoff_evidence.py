from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.crm_routing_reliability import _stage_action_supported
from apps.ai_engagement.services.engagement_instruction_runtime import (
    _condition_evidence_match,
    _strong_evidence_match,
)


class AuthoredHandoffEvidenceTests(SimpleTestCase):
    rule = (
        "Rule 2 — Call or human request:\n"
        "- When the lead clearly asks for a call, callback, demo, human, consultant, "
        "specialist, or meeting, move the lead to Call Requested in the current pipeline."
    )

    def supported(self, body):
        context = SimpleNamespace(
            pipeline={
                "id": "sales",
                "available_stages": [{
                    "id": "call-requested", "name": "Call Requested",
                    "pipeline_id": "sales", "is_current_pipeline": True,
                }],
            },
            stage={"id": "new-lead", "name": "New leads"},
            conversation={"messages": [{"direction": "inbound", "body": body}]},
        )
        return _stage_action_supported(
            action={"type": "pipeline_transition", "stage_shift": {"stage_id": "call-requested"}},
            context=context,
            runtime_policy={"crm": {"stage_shifting": [self.rule]}},
            qualification_state={}, latest_text=body,
        )

    def test_qualification_no_answer_does_not_negate_affirmative_handoff(self):
        for body in (
            "No. I want a callback tomorrow at 3 PM",
            "No. I want a consultant",
            "No. Please call me tomorrow at 3 PM",
            "No. Book a meeting",
            "Slow replies; WhatsApp; 11-30; No. Please call me tomorrow at 3 PM.",
            "Slow replies; WhatsApp; 11-30; No paid ads. Please call me tomorrow at 3 PM.",
            "Slow replies; WhatsApp; 11-30. I am not currently running paid ads. Please call me tomorrow at 3 PM.",
        ):
            with self.subTest(body=body):
                self.assertTrue(self.supported(body))

    def test_final_oxford_comma_alternative_is_supported(self):
        self.assertTrue(self.supported("20 leads per day. Book a meeting"))
        self.assertTrue(_strong_evidence_match("Book a meeting", "specialist or meeting"))

    def test_separate_answer_cannot_turn_denial_or_hypothetical_into_request(self):
        for body in (
            "No. Do not call me",
            "No. I don't want a callback",
            "No. I want a callback, but do not call me",
            "No. If I wanted a callback, I would tell you",
            "No. I might need a consultant later",
            "No. I declined the callback",
            "No. What would a callback involve?",
            "No demo please",
            "Do not contact me. Slow replies; WhatsApp; 11-30; No. Please call me tomorrow at 3 PM.",
            "No paid ads. Do not call me tomorrow at 3 PM.",
            "I am not running paid ads. I don't want a callback.",
            "No paid ads. If I wanted a callback, I would tell you.",
            "No paid ads. I might need a consultant later.",
        ):
            with self.subTest(body=body):
                self.assertFalse(self.supported(body))

    def test_boolean_and_sent_history_conditions_remain_required(self):
        self.assertFalse(_strong_evidence_match("Call me", "call and payment approved"))
        self.assertFalse(_strong_evidence_match("Call me", "call unless payment pending"))
        self.assertFalse(_strong_evidence_match("No paid ads. Call me", "call and payment approved"))
        self.assertFalse(_strong_evidence_match("No paid ads. Call me", "call and running paid ads"))
        condition = (
            "First Adviser has already been provided\n"
            "Second Adviser has already been provided\n"
            "The lead still requests assistance or says the issue remains unresolved"
        )
        context = SimpleNamespace(conversation={"messages": [
            {"direction": "outbound", "status": "sent", "body": "First Adviser — 2025550101"},
            {"direction": "outbound", "status": "queued", "body": "Second Adviser — 2025550102"},
        ]})
        self.assertFalse(_condition_evidence_match("I still need help", condition, context))
        context.conversation["messages"][1]["status"] = "delivered"
        self.assertTrue(_condition_evidence_match("I still need help", condition, context))
