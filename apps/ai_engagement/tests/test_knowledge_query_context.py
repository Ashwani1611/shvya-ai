"""Regressions for current-question retrieval and transport context."""
from dataclasses import replace

from django.test import SimpleTestCase

from apps.ai_engagement.services.context import AIContext, AIContextBuilder
from apps.ai_engagement.services.engagement import EngagementService


def context_for(messages, **metadata):
    return AIContext(
        organization={}, lead={}, pipeline={}, stage={}, contacts=[],
        attributes=[], conversation={"messages": messages, **metadata},
        conversation_summary=None, qualification_notes=[], knowledge=[],
    )


class KnowledgeQueryContextTests(SimpleTestCase):
    def test_long_assistant_reply_cannot_hide_new_customer_question(self):
        context = context_for([
            {"direction": "outbound", "body": "Our earlier discussion. " * 200},
            {"direction": "inbound", "body": "Can the Enterprise package export audit reports?"},
        ])
        query = EngagementService()._build_knowledge_query(context=context)
        self.assertTrue(query.startswith("Lead: Can the Enterprise package export audit reports?"))
        self.assertLessEqual(len(query), 900)

    def test_latest_customer_question_wins_over_later_outbound_message(self):
        context = context_for([
            {"direction": "inbound", "body": "What is your refund policy?"},
            {"direction": "outbound", "body": "Please share your budget." * 100},
        ])
        query = EngagementService()._build_knowledge_query(context=context)
        self.assertEqual(query, "Lead: What is your refund policy?")

    def test_short_followup_preserves_bounded_previous_context(self):
        context = context_for([
            {"direction": "outbound", "body": "Would you like the Enterprise pricing?"},
            {"direction": "inbound", "body": "Yes please"},
        ])
        query = EngagementService()._build_knowledge_query(context=context)
        self.assertTrue(query.startswith("Lead: Yes please"))
        self.assertIn("Enterprise pricing", query)
        self.assertLessEqual(len(query), 900)

    def test_no_customer_text_does_not_query_assistant_claims(self):
        context = context_for([{"direction": "outbound", "body": "Unverified earlier claim"}])
        self.assertEqual(EngagementService()._build_knowledge_query(context=context), "")
        context = replace(context, conversation={"messages": None})
        self.assertEqual(EngagementService()._build_knowledge_query(context=context), "")

    def test_channel_survives_compaction_without_replacing_acquisition_source(self):
        context = context_for(
            [{"direction": "inbound", "body": "Please send the brochure"}],
            channel="instagram", execution_mode="preview",
        )
        compact = EngagementService()._compact_conversation(context.conversation)
        self.assertEqual(compact["channel"], "instagram")
        self.assertEqual(compact["execution_mode"], "preview")
        self.assertEqual(compact["messages"], context.conversation["messages"])

    def test_whatsapp_builder_marks_actual_transport(self):
        conversation = AIContextBuilder()._build_conversation_context(messages=[])
        self.assertEqual(conversation["channel"], "whatsapp")
        self.assertEqual(conversation["execution_mode"], "live")
