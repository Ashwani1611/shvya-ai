from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.graph.evidence import _safe_unknown_decision
from apps.ai_engagement.services.conversation_policy import (
    ConversationPolicyDecision, ConversationPolicyOutcome,
)
from apps.ai_engagement.services.conversation_policy_runtime import _POLICY
from apps.ai_engagement.services.engagement import EngagementDecision


class RejectedQualificationContinuationTests(SimpleTestCase):
    def state(self):
        return {
            "context": SimpleNamespace(
                organization={"bot_languages": "English"},
                stage={"name": "New leads"},
            ),
            "latest_text": "Alex QA",
            "latest_message_id": "m2",
            "requirements": [
                {"id": "name", "question": "Your name?", "priority": 1},
                {"id": "age", "question": "Please provide your Age.",
                 "priority": 2, "can_direct_ask": True},
            ],
            "qualification_state": {
                "engagement_mode": "qualification",
                "qualification_status": "in_progress",
                "requirement_states": {
                    "name": {"status": "answered", "source_message_id": "m2"},
                    "age": {"status": "unknown"},
                },
            },
        }

    def recover(self, state, outcome=ConversationPolicyOutcome.ASK_QUALIFICATION,
                next_id="age", continue_flow=True, failure_reason="unanswered_question"):
        policy = ConversationPolicyDecision(
            outcome=outcome, reason_code="TEST", confidence=1,
            continue_qualification=continue_flow, next_requirement_id=next_id,
        )
        token = _POLICY.set(policy)
        try:
            decision = EngagementDecision(
                should_engage=True, message="Your booking is confirmed.",
                file_document_id=7, crm_actions=[{"type": "pipeline_transition"}],
                reason="QUALIFICATION_NEXT", next_requirement_id="age", model="test",
                qualification_updates=[{"requirement_id": "untrusted"}],
            )
            return _safe_unknown_decision(
                decision, qualification_turn=True, state=state,
                failure_reason=failure_reason,
            )
        finally:
            _POLICY.reset(token)

    def test_keeps_exact_backend_question_after_captured_answer_without_effects(self):
        result = self.recover(self.state())
        self.assertTrue(result.message.endswith("\n\nPlease provide your Age."))
        self.assertEqual(result.next_requirement_id, "age")
        self.assertEqual(result.qualification_updates, [])
        self.assertEqual(result.crm_actions, [])
        self.assertIsNone(result.file_document_id)
        self.assertNotIn("confirmed", result.message)

    def test_does_not_continue_for_handoff_or_policy_mismatch(self):
        for kwargs in (
            {"outcome": ConversationPolicyOutcome.HUMAN_HANDOFF, "continue_flow": False},
            {"next_id": "another"},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(self.recover(self.state(), **kwargs).next_requirement_id)

    def test_does_not_reuse_unaccepted_or_historical_answer(self):
        state = self.state()
        state["qualification_state"]["requirement_states"]["name"]["source_message_id"] = "old"
        self.assertIsNone(self.recover(state).next_requirement_id)

    def test_does_not_override_language_or_later_stage(self):
        for language, stage in (("Hindi", "New leads"), ("English", "Qualified")):
            state = self.state()
            state["context"].organization["bot_languages"] = language
            state["context"].stage["name"] = stage
            self.assertIsNone(self.recover(state).next_requirement_id)

    def test_factual_request_does_not_turn_into_question_only(self):
        state = self.state()
        state["latest_text"] = "What is the membership price?"
        self.assertIsNone(self.recover(state).next_requirement_id)

    def test_non_wording_rejections_cannot_use_continuation_recovery(self):
        for reason in ("unsupported_claim", "invalid_qualification", "invalid_file", "provider_error"):
            with self.subTest(reason=reason):
                self.assertIsNone(self.recover(self.state(), failure_reason=reason).next_requirement_id)
