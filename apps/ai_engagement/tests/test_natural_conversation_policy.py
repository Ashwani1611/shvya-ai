from django.test import SimpleTestCase

from apps.ai_engagement.graph.policy_actions import (
    _resolve_existing_attribute_key,
    _value_supported_by_definition,
)
from apps.ai_engagement.services.conversation_policy import (
    ConversationPolicyContext,
    ConversationPolicyEngine,
    ConversationPolicyOutcome,
)
from apps.ai_engagement.services.intent_types import (
    ClassificationPath,
    Intent,
    IntentDecision,
)


class NaturalConversationPolicyTests(SimpleTestCase):
    def _context(self, decision, *, next_requirement_id="q2"):
        return ConversationPolicyContext(
            intent_decision=decision,
            organization_id="org",
            lead_id="lead",
            qualification_state={"qualification_status": "in_progress"},
            qualification_result={
                "accepted": True,
                "source_message_id": "msg-1",
            },
            next_requirement_id=next_requirement_id,
        )

    def test_pure_qualification_answer_can_continue_naturally(self):
        decision = IntentDecision(
            primary_intent=Intent.QUALIFICATION_ANSWER,
            confidence=0.99,
            classification_path=ClassificationPath.DETERMINISTIC,
        )
        result = ConversationPolicyEngine().decide(self._context(decision))
        self.assertEqual(result.outcome, ConversationPolicyOutcome.ASK_QUALIFICATION)
        self.assertEqual(result.next_requirement_id, "q2")

    def test_parallel_conversation_context_does_not_force_next_question(self):
        decision = IntentDecision(
            primary_intent=Intent.QUALIFICATION_ANSWER,
            secondary_intents=(Intent.OBJECTION,),
            confidence=0.95,
            classification_path=ClassificationPath.MODEL,
        )
        result = ConversationPolicyEngine().decide(self._context(decision))
        self.assertEqual(result.outcome, ConversationPolicyOutcome.NORMAL_CONVERSATION)
        self.assertIsNone(result.next_requirement_id)
        self.assertFalse(result.continue_qualification)

    def test_direct_customer_question_owns_turn_even_when_answer_is_accepted(self):
        decision = IntentDecision(
            primary_intent=Intent.PRICING_QUESTION,
            secondary_intents=(Intent.QUALIFICATION_ANSWER,),
            confidence=0.96,
            direct_question="How much does it cost?",
            classification_path=ClassificationPath.MODEL,
            requires_knowledge=True,
        )
        result = ConversationPolicyEngine().decide(self._context(decision))
        self.assertEqual(result.outcome, ConversationPolicyOutcome.ANSWER)
        self.assertTrue(result.answer_customer_question)
        self.assertFalse(result.continue_qualification)
        self.assertIsNone(result.next_requirement_id)


class ConversationalAttributeResolutionTests(SimpleTestCase):
    def test_semantic_alias_reuses_existing_attribute(self):
        definitions = [
            {
                "key": "number_of_sales_representatives",
                "name": "Number of Sales Representatives",
                "field_type": "numeric",
                "options": [],
            }
        ]
        self.assertEqual(
            _resolve_existing_attribute_key("new:Sales Team Size", definitions),
            "number_of_sales_representatives",
        )

    def test_numeric_customer_value_can_match_option_range(self):
        definition = {
            "key": "leads_per_day",
            "name": "Leads/d",
            "field_type": "option",
            "options": ["0-10", "10-30", "30+"],
        }
        self.assertTrue(
            _value_supported_by_definition("10-30", "We get around 19 leads every day.", definition)
        )
        self.assertFalse(
            _value_supported_by_definition("30+", "We get around 19 leads every day.", definition)
        )
