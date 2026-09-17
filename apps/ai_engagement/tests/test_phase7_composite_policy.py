from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.conversation_policy import (
    ConversationPolicyContext,
    ConversationPolicyEngine,
    ConversationPolicyOutcome,
)
from apps.ai_engagement.services.intent_engine import (
    ClassificationPath,
    Intent,
    IntentDecision,
)
from apps.ai_engagement.services.phase7_completion_runtime import _decision_for_planning


class Phase7CompositePolicyTests(SimpleTestCase):
    def _intent(self):
        return IntentDecision(
            primary_intent=Intent.PRICING_QUESTION,
            secondary_intents=(Intent.QUALIFICATION_ANSWER, Intent.CALL_REQUEST),
            confidence=0.99,
            facts=(
                {
                    "requirement_id": "volume",
                    "key": "lead_volume",
                    "value": 30,
                    "confidence": 0.99,
                    "source_message_id": "master-message",
                },
            ),
            direct_question="What is your pricing?",
            qualification_candidate={
                "requirement_id": "volume",
                "key": "lead_volume",
                "value": 30,
                "confidence": 0.99,
                "evidence": "around 30 leads daily",
            },
            requested_action="call",
            classification_path=ClassificationPath.DETERMINISTIC,
            requires_knowledge=True,
            requires_human=True,
            model="deterministic",
        )

    def _context(self, channel):
        decision = self._intent()
        return ConversationPolicyContext(
            intent_decision=decision,
            organization_id="org-a",
            lead_id="lead-a",
            pipeline_id="pipeline-a",
            stage_id="new-lead",
            qualification_state={
                "requirement_states": {
                    "volume": {
                        "status": "answered",
                        "value": 30,
                        "source_message_id": "master-message",
                    },
                    "ads": {"status": "unknown"},
                },
                "next_requirement_id": "ads",
            },
            qualification_result={
                "accepted": True,
                "source_message_id": "master-message",
            },
            next_requirement_id="ads",
            extracted_facts=tuple(decision.facts),
            knowledge_available=True,
            capabilities=frozenset({"call"}),
            channel=channel,
        )

    def test_master_policy_answers_pricing_continues_qualification_and_preserves_call(self):
        results = []
        for channel in ("api", "hosted", "coexistence"):
            results.append(ConversationPolicyEngine().decide(self._context(channel)))

        self.assertEqual(results[0].as_dict(), results[1].as_dict())
        self.assertEqual(results[1].as_dict(), results[2].as_dict())
        policy = results[0]
        self.assertEqual(policy.outcome, ConversationPolicyOutcome.ANSWER_THEN_QUALIFY)
        self.assertTrue(policy.answer_customer_question)
        self.assertTrue(policy.continue_qualification)
        self.assertEqual(policy.next_requirement_id, "ads")
        self.assertTrue(policy.requires_knowledge)
        self.assertTrue(policy.requires_human)
        self.assertEqual(policy.handoff_type, "call")

    def test_call_without_grounded_time_becomes_proposal_only_handoff(self):
        turn = {"intent_decision": self._intent()}
        original = SimpleNamespace(
            crm_actions=[],
            file_document_id=None,
            reason_code="NORMAL_CONVERSATION",
        )
        planned = _decision_for_planning(original, turn)
        self.assertEqual(planned.reason_code, "HUMAN_HANDOFF")
        self.assertEqual(planned.crm_actions, [])

    def test_grounded_reminder_is_not_replaced_by_handoff_fallback(self):
        turn = {"intent_decision": self._intent()}
        original = SimpleNamespace(
            crm_actions=[
                {
                    "type": "create_reminder",
                    "title": "Call lead",
                    "description": "Lead requested a call.",
                    "due_at": "2026-09-18T17:00:00+05:30",
                }
            ],
            file_document_id=None,
            reason_code="NORMAL_CONVERSATION",
        )
        self.assertIs(_decision_for_planning(original, turn), original)
