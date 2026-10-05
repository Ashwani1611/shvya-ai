"""Fallbacks and persisted qualification plans cannot override this turn's intent."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.conversation_policy import ConversationPolicyDecision, ConversationPolicyOutcome
from apps.ai_engagement.services.conversation_policy_runtime import _POLICY, _TURN
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.engagement_failsoft import build_deterministic_fallback_decision
from apps.ai_engagement.services.qualification_execution.finalization import _finalize
from tests.playbook_fixtures import build_ai_playbook


_QUESTION = "Where do you currently manage your leads?\nA. WhatsApp\nB. Excel"
_PLAN = {"response_type": "qualification_progress", "acknowledgement_required": False,
         "next_requirement": {"id": "tools", "rendered": _QUESTION}}


@contextmanager
def current_policy(*, outcome=ConversationPolicyOutcome.ANSWER, source="current", organization="org", lead="lead"):
    turn = _TURN.set({"organization_id": organization, "lead_id": lead, "source_message_id": source})
    policy = _POLICY.set(ConversationPolicyDecision(outcome=outcome, reason_code="DIRECT_CUSTOMER_QUESTION",
        confidence=1, answer_customer_question=outcome == ConversationPolicyOutcome.ANSWER,
        continue_qualification=False, allowed_response_goal="answer_customer_question"))
    try:
        yield
    finally:
        _POLICY.reset(policy)
        _TURN.reset(turn)


class FailureResponsePriorityTests(SimpleTestCase):
    def setUp(self):
        self.org = SimpleNamespace(id="org", pk="org", name="Example")
        self.lead = SimpleNamespace(id="lead", pk="lead", attributes={}, pipeline=None,
                                    stage=SimpleNamespace(name="New leads"))
        self.info = SimpleNamespace(ai_playbook=build_ai_playbook(questions=_QUESTION),
            about="# Pricing\nDIY plan costs ₹2,999 per month per user.", bot_languages="English")

    def fallback(self, text, *, source_id="current", plan=None):
        source = SimpleNamespace(id=source_id, body=text,
            raw_payload={"shvya_ai_processing": {"qualification_response_plan": plan or _PLAN}})
        with patch("apps.ai_engagement.models.OrgInfo.objects.filter") as info_query, \
             patch("apps.ai_engagement.services.authored_knowledge.matching_authored_answers", return_value=[]):
            info_query.return_value.only.return_value.first.return_value = self.info
            return build_deterministic_fallback_decision(organization=self.org, lead=self.lead, latest_inbound=source)

    @staticmethod
    def reply():
        return EngagementDecision(should_engage=True, message="DIY plan costs ₹2,999 per month per user.",
            file_document_id=None, crm_actions=[], reason="ANSWER_ORG_QUESTION", reason_code="ANSWER_ORG_QUESTION",
            model="deterministic-fallback")

    def test_combined_pricing_request_outranks_persisted_qualification_progress(self):
        text = ("My main problem is slow replies. I manage leads in Excel, receive 20 leads per day, "
                "and I am currently running paid ads. Please share your product brochure and tell me the DIY plan price.")
        with current_policy():
            result = self.fallback(text)
            final = _finalize(result, {"source_message_id": "current", "response_plan": _PLAN})
        self.assertIn("₹2,999", final.message)
        self.assertNotIn(_QUESTION.splitlines()[0], final.message)
        self.assertEqual(final.reason_code, "ANSWER_ORG_QUESTION")
        self.assertIsNone(final.next_requirement_id)
        self.assertEqual(final.crm_actions, [])
        self.assertEqual(final.qualification_updates, [])
        self.assertIsNone(final.file_document_id)

    def test_explicit_question_interrupts_legacy_plan_without_active_policy(self):
        with current_policy(source="other-turn"):
            result = self.fallback("What is the DIY price?")
        self.assertIn("₹2,999", result.message)
        self.assertIsNone(result.next_requirement_id)

    def test_answer_policy_blocks_next_question_even_without_keyword_match(self):
        with current_policy():
            result = self.fallback("Tell me more")
        self.assertNotEqual(result.reason_code, "QUALIFICATION_NEXT")
        self.assertIsNone(result.next_requirement_id)
        self.assertNotIn(_QUESTION.splitlines()[0], result.message)

    def test_other_source_or_tenant_policy_cannot_suppress_qualification(self):
        for scope in ({"source": "other"}, {"organization": "other"}, {"lead": "other"}):
            with self.subTest(scope=scope), current_policy(**scope):
                result = self.fallback("B")
            self.assertEqual(result.reason_code, "QUALIFICATION_NEXT")
            self.assertEqual(result.next_requirement_id, "tools")

    def test_finalizer_preserves_answer_and_handoff_across_old_plan_types(self):
        for outcome in (ConversationPolicyOutcome.ANSWER, ConversationPolicyOutcome.CALL_HANDOFF,
                        ConversationPolicyOutcome.HUMAN_HANDOFF, ConversationPolicyOutcome.BOOKING_FLOW):
            for kind in ("qualification_start", "qualification_progress", "qualification_clarification", "qualification_complete"):
                with self.subTest(outcome=outcome, plan=kind), current_policy(outcome=outcome):
                    decision = self.reply()
                    state = {"source_message_id": "current", "response_plan": {**_PLAN, "response_type": kind}}
                    self.assertIs(_finalize(decision, state), decision)

    def test_finalizer_keeps_other_source_and_completion_behavior(self):
        with current_policy(source="other"):
            final = _finalize(self.reply(), {"source_message_id": "current", "response_plan": _PLAN})
        self.assertIn(_QUESTION, final.message)
        self.assertEqual(final.next_requirement_id, "tools")
        with current_policy(outcome=ConversationPolicyOutcome.NORMAL_CONVERSATION):
            final = _finalize(self.reply(), {"source_message_id": "current", "response_plan": {
                "response_type": "qualification_complete", "final_configured_acknowledgement": {"value": "Thanks for sharing your details."}}})
        self.assertIn("Thanks for sharing your details.", final.message)
        self.assertIsNone(final.next_requirement_id)
