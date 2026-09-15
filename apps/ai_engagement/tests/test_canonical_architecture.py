from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import Chunk, Document
from apps.ai_engagement.services.canonical_architecture import (
    PolicyDecisionResolver,
    ResponseActionValidator,
)
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.embeddings import EmbeddingError
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.retrieval import KnowledgeRetrievalService
from apps.organizations.models import Organization


class CanonicalKnowledgeRetrievalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Hybrid Knowledge Org")
        cls.other_organization = Organization.objects.create(name="Other Tenant")

        cls.website = Document.objects.create(
            organization=cls.organization,
            name="Phoenix Enterprise Pricing",
            source_key="https://example.com/pricing",
            source_url="https://example.com/pricing",
            version=1,
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        cls.website_chunk = Chunk.objects.create(
            organization=cls.organization,
            document=cls.website,
            chunk_index=0,
            content=(
                "The Phoenix Enterprise plan includes WhatsApp automation and "
                "priority onboarding. Phoenix Enterprise pricing starts at 4999."
            ),
            is_active=True,
        )

        cls.other_document = Document.objects.create(
            organization=cls.other_organization,
            name="Phoenix Enterprise Pricing",
            source_key="other-pricing.txt",
            version=1,
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        Chunk.objects.create(
            organization=cls.other_organization,
            document=cls.other_document,
            chunk_index=0,
            content="Phoenix Enterprise pricing for another tenant is 1.",
            is_active=True,
        )

        cls.inactive_document = Document.objects.create(
            organization=cls.organization,
            name="Old Phoenix Pricing",
            source_key="old-pricing.txt",
            version=1,
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=False,
        )
        Chunk.objects.create(
            organization=cls.organization,
            document=cls.inactive_document,
            chunk_index=0,
            content="Old Phoenix Enterprise pricing was 100.",
            is_active=True,
        )

    def test_keyword_retrieval_is_tenant_scoped_and_excludes_inactive_documents(self):
        results = KnowledgeRetrievalService().retrieve_by_keyword(
            organization=self.organization,
            query_text="Phoenix Enterprise pricing",
            limit=5,
        )

        self.assertTrue(results)
        self.assertEqual(results[0].chunk.id, self.website_chunk.id)
        self.assertTrue(all(item.chunk.organization_id == self.organization.id for item in results))
        self.assertNotIn(
            self.inactive_document.id,
            {item.chunk.document_id for item in results},
        )
        self.assertIn("keyword", results[0].retrieval_methods)

    def test_hybrid_retrieval_can_run_keyword_only_when_vector_is_unavailable(self):
        results = KnowledgeRetrievalService().retrieve_hybrid(
            organization=self.organization,
            query_text="Phoenix Enterprise plan pricing",
            query_vector=None,
            limit=3,
        )

        self.assertTrue(results)
        self.assertEqual(results[0].chunk.id, self.website_chunk.id)
        self.assertGreater(results[0].similarity, 0.38)
        self.assertEqual(results[0].distance, None)

    @patch(
        "apps.ai_engagement.services.canonical_architecture.EmbeddingService.embed_text",
        side_effect=EmbeddingError("embedding provider unavailable"),
    )
    def test_context_builder_falls_back_to_verified_keyword_evidence(self, _embed):
        knowledge = AIContextBuilder()._build_knowledge_context(
            organization=self.organization,
            knowledge_query="Phoenix Enterprise pricing",
            query_vector=None,
            limit=3,
        )

        self.assertTrue(knowledge)
        self.assertEqual(knowledge[0]["document_id"], str(self.website.id))
        self.assertEqual(knowledge[0]["source_type"], "website")
        self.assertIn("keyword", knowledge[0]["retrieval_methods"])
        self.assertIn("4999", knowledge[0]["content"])


class CanonicalDecisionResolverTests(SimpleTestCase):
    def _decision(self, **overrides):
        data = {
            "should_engage": True,
            "message": "Thanks.",
            "file_document_id": None,
            "crm_actions": [],
            "reason": "NORMAL_CONVERSATION",
            "model": "test",
            "reason_code": "NORMAL_CONVERSATION",
            "qualification_updates": [],
        }
        data.update(overrides)
        return EngagementDecision(**data)

    def test_structured_decision_separates_attribute_and_workflow_actions(self):
        decision = self._decision(
            crm_actions=[
                {
                    "type": "attribute_updates",
                    "updates": [{"key": "lead_volume", "value": "10-30"}],
                },
                {
                    "type": "create_reminder",
                    "title": "Follow up",
                    "description": "Lead asked for a call",
                    "due_at": "2026-09-17T10:00:00+05:30",
                },
            ],
            qualification_updates=[
                {"requirement_id": "daily_leads", "value": "10-30"}
            ],
        )
        context = type(
            "Context",
            (),
            {
                "conversation": {
                    "messages": [
                        {"id": "source-1", "direction": "inbound", "body": "B"}
                    ]
                }
            },
        )()

        structured = PolicyDecisionResolver().resolve(
            decision=decision,
            context=context,
            controlled_actions=decision.crm_actions,
            policy_result={"evaluation": {"outcome": "in_progress"}},
        )

        self.assertEqual(
            structured.attribute_updates,
            [{"key": "lead_volume", "value": "10-30"}],
        )
        self.assertEqual(structured.workflow_actions[0]["type"], "create_reminder")
        self.assertEqual(structured.source_message_id, "source-1")
        self.assertEqual(structured.qualification_outcome, "in_progress")


class FinalResponseActionValidatorTests(SimpleTestCase):
    def _decision(self, message, *, file_document_id=None):
        return EngagementDecision(
            should_engage=True,
            message=message,
            file_document_id=file_document_id,
            crm_actions=[],
            reason="NORMAL_CONVERSATION",
            model="test",
            reason_code="NORMAL_CONVERSATION",
        )

    def test_pending_file_action_cannot_claim_completed_send(self):
        decision = self._decision(
            "Yes, that plan is available. I've shared the brochure with you.",
            file_document_id=12,
        )
        reconciled = {
            "stage": {"name": "New Lead"},
            "qualification": {"status": "in_progress"},
            "workflow": {"action_types": []},
            "file_share": {
                "document_id": 12,
                "document_name": "Pricing Brochure",
                "status": "resolved_pending_send",
            },
        }

        result = ResponseActionValidator().validate(
            decision=decision,
            reconciled_state=reconciled,
        )

        self.assertNotIn("I've shared", result.message)
        self.assertIn("I'm sending Pricing Brochure with this message.", result.message)
        self.assertEqual(result.file_document_id, 12)

    def test_confirmed_file_send_may_be_described_as_completed(self):
        decision = self._decision(
            "I've shared the brochure with you.",
            file_document_id=12,
        )
        reconciled = {
            "stage": {"name": "New Lead"},
            "qualification": {"status": "in_progress"},
            "workflow": {"action_types": []},
            "file_share": {
                "document_id": 12,
                "document_name": "Pricing Brochure",
                "status": "sent",
            },
        }

        result = ResponseActionValidator().validate(
            decision=decision,
            reconciled_state=reconciled,
        )

        self.assertEqual(result.message, decision.message)

    def test_unconfirmed_booking_claim_is_replaced(self):
        decision = self._decision("Your demo is confirmed for tomorrow.")
        reconciled = {
            "stage": {"name": "New Lead"},
            "qualification": {"status": "in_progress"},
            "workflow": {
                "action_types": [],
                "booking_status": "user_reported",
                "booking_confirmation": None,
            },
            "file_share": {"status": "none"},
        }

        result = ResponseActionValidator().validate(
            decision=decision,
            reconciled_state=reconciled,
        )

        self.assertIn("isn't confirmed yet", result.message)
        self.assertNotEqual(result.message, decision.message)

    def test_unexecuted_reminder_claim_is_replaced(self):
        decision = self._decision("I've set a reminder to call you tomorrow.")
        reconciled = {
            "stage": {"name": "New Lead"},
            "qualification": {"status": "in_progress"},
            "workflow": {"action_types": []},
            "file_share": {"status": "none"},
        }

        result = ResponseActionValidator().validate(
            decision=decision,
            reconciled_state=reconciled,
        )

        self.assertEqual(result.message, "I've noted the follow-up request.")
