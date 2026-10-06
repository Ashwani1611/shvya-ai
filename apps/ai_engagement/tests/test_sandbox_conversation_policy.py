from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.conversation_policy import ConversationPolicyOutcome
from apps.ai_engagement.services.intent_types import Intent, IntentDecision
from apps.ai_engagement.services.playground import _SandboxLead
from apps.ai_engagement.services.qualification_state import record_last_asked_requirement, state_for_lead
from apps.ai_engagement.services.sandbox_conversation_policy import sandbox_policy


class SandboxConversationPolicyTests(SimpleTestCase):
    def setUp(self):
        self.organization = SimpleNamespace(pk="org", settings={})
        self.lead = _SandboxLead(
            pk="playground:test", id="playground:test", attributes={},
            stage=SimpleNamespace(name="New leads"), stage_id=None,
            pipeline=None, pipeline_id=None,
        )
        self.requirements = [
            {"id": "name", "question": "Please provide your Full Name.", "priority": 1},
            {"id": "age", "question": "Please provide your Age.", "priority": 2},
            {"id": "date", "question": "Please provide your Preferred Date.", "priority": 3},
        ]
        record_last_asked_requirement(self.lead, "name", requirements=self.requirements)

    def policy(self, message, primary=Intent.QUALIFICATION_ANSWER, source="playground:test:turn:2"):
        with patch("apps.ai_engagement.services.sandbox_conversation_policy._capabilities", return_value=frozenset()):
            return sandbox_policy(
                organization=self.organization, lead=self.lead,
                intent=IntentDecision(primary_intent=primary, confidence=1),
                requirements=self.requirements, message=message,
                source_message_id=source,
            )

    def test_name_and_age_advance_under_shared_policy_with_inbound_evidence(self):
        policy = self.policy("Alex QA")
        self.assertEqual(policy.outcome, ConversationPolicyOutcome.ASK_QUALIFICATION)
        self.assertEqual(policy.next_requirement_id, "age")
        state = state_for_lead(self.lead, requirements=self.requirements)
        self.assertEqual(state["requirement_states"]["name"]["value"], "Alex QA")
        self.assertEqual(state["requirement_states"]["name"]["source_message_id"], "playground:test:turn:2")
        record_last_asked_requirement(self.lead, "age", requirements=self.requirements)
        policy = self.policy("I am 28 years old.", source="playground:test:turn:3")
        self.assertEqual(policy.next_requirement_id, "date")
        self.assertFalse(state_for_lead(self.lead, requirements=self.requirements)["qualification_completed"])

    def test_human_request_does_not_become_an_answer_or_next_question(self):
        policy = self.policy("I want a real human", primary=Intent.HUMAN_REQUEST)
        self.assertEqual(policy.outcome, ConversationPolicyOutcome.HUMAN_HANDOFF)
        self.assertFalse(policy.continue_qualification)
        self.assertEqual(state_for_lead(self.lead, requirements=self.requirements)["answered_requirement_ids"], [])

    def test_opt_out_is_not_captured_as_a_name(self):
        policy = self.policy("Stop", primary=Intent.OPT_OUT)
        self.assertEqual(policy.outcome, ConversationPolicyOutcome.OPT_OUT)
        self.assertEqual(state_for_lead(self.lead, requirements=self.requirements)["answered_requirement_ids"], [])

    def test_later_stage_does_not_capture_or_restart_qualification(self):
        self.lead.stage.name = "Qualified"
        policy = self.policy("Alex QA")
        self.assertFalse(policy.continue_qualification)
        self.assertEqual(state_for_lead(self.lead, requirements=self.requirements)["answered_requirement_ids"], [])

    def test_real_orm_lead_and_wrong_session_source_are_rejected(self):
        self.assertIsNone(self.policy("Alex QA", source="playground:other:turn:2"))
        self.lead._meta = object()
        self.assertIsNone(self.policy("Alex QA"))

    def test_an_unasked_question_cannot_be_captured(self):
        self.lead.attributes = {}
        self.policy("Alex QA")
        self.assertEqual(state_for_lead(self.lead, requirements=self.requirements)["answered_requirement_ids"], [])
