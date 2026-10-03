"""Installed-runtime regressions; provider transport is not exercised here."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph import workflow
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.engagement import EngagementError, EngagementService
from apps.ai_engagement.services.file_sharing import FileSharingService
from apps.ai_engagement.tests.test_conversation_continuity_contract import INTRO, inbound, outbound


class ConversationContinuityIntegrationTests(SimpleTestCase):
    def setUp(self):
        self.context = AIContext(
            organization={"id": "f87c7d8b-11f4-42df-a0b8-532db7fca645", "name": "Example CRM"},
            lead={"id": "3dba7b4b-0916-4b41-b2df-e6f0d08fbf5e", "attributes": {}},
            pipeline={}, stage={"name": "Qualified"}, contacts=[], attributes=[],
            conversation={"channel": "whatsapp", "execution_mode": "live", "messages": []},
            conversation_summary=None, qualification_notes=[], knowledge=[],
        )
        self.service = EngagementService()

    def with_messages(self, *messages):
        return replace(self.context, conversation={**self.context.conversation, "messages": list(messages)})

    def test_installed_query_keeps_pricing_before_repeated_brochure_requests(self):
        ctx = self.with_messages(outbound("Thank you"), inbound("What is the product and its pricing and package?", "price"),
                                 *[inbound("Share brochure", f"file-{i}") for i in range(6)])
        query = self.service._build_knowledge_query(context=ctx)
        self.assertIn("pricing and package", query)
        self.assertIn("Share brochure", query)
        self.assertLessEqual(len(query), 900)

    def test_installed_retrieval_recognizes_request_before_please(self):
        ctx = self.with_messages(inbound("pricing", "price"), inbound("please", "nudge"))
        self.assertTrue(self.service._should_retrieve_knowledge(context=ctx))
        self.assertEqual(self.service._latest_inbound_message_id(context=ctx), "nudge")

    def test_installed_graph_routes_mixed_qualification_turn_to_retrieval(self):
        ctx = self.with_messages(inbound("What is pricing?", "price"), inbound("No", "answer"))
        result = workflow._route_turn({"context": ctx, "answer_extracted": True, "service": self.service})
        self.assertEqual(result["route"], "rag")
        self.assertIn("pricing", result["retrieval_query"])
        self.assertEqual(self.service._latest_inbound_message_id(context=ctx), "answer")

    def test_qualified_stage_does_not_authorize_model_silence(self):
        with self.assertRaises(EngagementError):
            self.service._validate_engagement_policy(
                decision=SimpleNamespace(should_engage=False, message=""), context=self.with_messages(inbound("pricing")))

    def test_qualified_stage_allows_normal_sales_reply(self):
        self.service._validate_engagement_policy(
            decision=SimpleNamespace(should_engage=True, message="Here are the approved product details."),
            context=self.with_messages(inbound("What is the product?")))

    def test_duplicate_intro_rejected_by_installed_policy(self):
        with self.assertRaisesRegex(EngagementError, "already sent"):
            self.service._validate_engagement_policy(
                decision=SimpleNamespace(should_engage=True, message=INTRO),
                context=self.with_messages(outbound(INTRO), inbound("Hello")))

    def test_pending_explicit_brochure_survives_following_nudge(self):
        ctx = self.with_messages(inbound("Send brochure", "request"), inbound("please", "nudge"))
        ctx = replace(ctx, lead={**ctx.lead, "shared_document_ids": [7]})
        document = SimpleNamespace(id=7, name="Product brochure", version=1, source_url="",
                                   share_instruction="Send when the customer requests the product brochure.")
        with patch.object(FileSharingService, "get_eligible_documents", return_value=[document]):
            candidates = FileSharingService().build_file_candidates(organization=object(), context=ctx)
        self.assertEqual([item["document_id"] for item in candidates], [7])
        self.assertEqual(self.service._latest_inbound_message_id(context=ctx), "nudge")

    def test_hosted_send_interval_remains_45_seconds(self):
        from services.channels.ai_send_gate import AI_SEND_GAP_SECONDS
        self.assertEqual(AI_SEND_GAP_SECONDS, 45)
