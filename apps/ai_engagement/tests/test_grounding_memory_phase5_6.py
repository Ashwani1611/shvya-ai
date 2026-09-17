from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.graph.evidence import check_grounding
from apps.ai_engagement.models import Chunk, Document
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.evidence_resolver import (
    EvidenceResolution,
    EvidenceResolver,
    GroundingCategory,
    InformationClass,
)
from apps.ai_engagement.services.intent_types import (
    ClassificationPath,
    Intent,
    IntentDecision,
)
from apps.ai_engagement.services import phase5_6_runtime
from apps.ai_engagement.services.structured_memory import (
    MEMORY_KEY,
    StructuredLeadMemoryService,
    StructuredMemoryScopeError,
)
from apps.ai_engagement.services.tenant_guard import TenantScopeError
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class Phase56GroundingMemoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(
            name="Org A",
            settings={
                "pricing": {"pro": "₹999/month"},
                "policies": {"refund": "Refunds are reviewed within 7 days."},
                "locations": ["Delhi"],
                "working_hours": "09:00-18:00",
                "services": ["Sales automation"],
            },
        )
        cls.org_b = Organization.objects.create(
            name="Org B",
            settings={
                "pricing": {"enterprise": "ORG_B_SECRET_PRICE"},
                "policies": {"refund": "ORG_B_PRIVATE_POLICY"},
                "locations": ["ORG_B_PRIVATE_LOCATION"],
            },
        )
        cls.org_empty = Organization.objects.create(name="Org Empty", settings={})

        cls.pipeline_a = Pipeline.objects.create(
            organization=cls.org_a,
            name="A Sales",
            country_code="+91",
            phone_number="9000000011",
        )
        cls.pipeline_b = Pipeline.objects.create(
            organization=cls.org_b,
            name="B Sales",
            country_code="+91",
            phone_number="9000000012",
        )
        cls.pipeline_empty = Pipeline.objects.create(
            organization=cls.org_empty,
            name="Empty Sales",
            country_code="+91",
            phone_number="9000000013",
        )
        cls.lead_a = Lead.objects.create(
            organization=cls.org_a,
            pipeline=cls.pipeline_a,
            stage=cls.pipeline_a.stages.get(name="New leads"),
            name="Lead A",
            phone="+919876543210",
        )
        # Deliberately use the same phone in another organization. Memory identity
        # must remain organization_id + lead_id, never phone number.
        cls.lead_b = Lead.objects.create(
            organization=cls.org_b,
            pipeline=cls.pipeline_b,
            stage=cls.pipeline_b.stages.get(name="New leads"),
            name="Lead B",
            phone="+919876543210",
        )
        cls.lead_empty = Lead.objects.create(
            organization=cls.org_empty,
            pipeline=cls.pipeline_empty,
            stage=cls.pipeline_empty.stages.get(name="New leads"),
            name="Lead Empty",
            phone="+919999999999",
        )

        cls.document_a = Document.objects.create(
            organization=cls.org_a,
            name="A Policy",
            source_key="a-policy",
            file="ai_knowledge/a-policy.txt",
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        cls.document_b = Document.objects.create(
            organization=cls.org_b,
            name="B Policy",
            source_key="b-policy",
            file="ai_knowledge/b-policy.txt",
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        cls.chunk_a = Chunk.objects.create(
            document=cls.document_a,
            organization=cls.org_a,
            content="Org A cancellation policy is available to current customers.",
            chunk_index=0,
        )
        cls.chunk_b = Chunk.objects.create(
            document=cls.document_b,
            organization=cls.org_b,
            content="ORG_B_KNOWLEDGE_SECRET refund details.",
            chunk_index=0,
        )

    @staticmethod
    def _intent(primary: Intent, *secondary: Intent, direct_question=None):
        return IntentDecision(
            primary_intent=primary,
            secondary_intents=tuple(secondary),
            confidence=0.99,
            direct_question=direct_question,
            classification_path=ClassificationPath.DETERMINISTIC,
            requires_knowledge=True,
        )

    def test_pricing_uses_only_current_org_structured_data(self):
        result = EvidenceResolver().resolve(
            organization=self.org_a,
            lead=self.lead_a,
            question="What is your price?",
            intent_decision=self._intent(Intent.PRICING_QUESTION),
        )
        self.assertEqual(result.category, GroundingCategory.STRUCTURED_ORG_DATA)
        self.assertTrue(result.verified)
        serialized = str(result.prompt_dict())
        self.assertIn("₹999/month", serialized)
        self.assertNotIn("ORG_B_SECRET_PRICE", serialized)
        self.assertTrue(all(item.score == 1.0 for item in result.evidence))

    def test_missing_sensitive_fact_returns_explicit_no_verified_evidence(self):
        result = EvidenceResolver().resolve(
            organization=self.org_empty,
            lead=self.lead_empty,
            question="What is your pricing?",
            intent_decision=self._intent(Intent.PRICING_QUESTION),
        )
        self.assertEqual(result.category, GroundingCategory.NO_VERIFIED_EVIDENCE)
        self.assertFalse(result.verified)
        self.assertTrue(result.sensitive)
        self.assertIn("don't want to guess", result.controlled_fallback)

    def test_resolver_rejects_cross_tenant_lead(self):
        with self.assertRaises(TenantScopeError):
            EvidenceResolver().resolve(
                organization=self.org_a,
                lead=self.lead_b,
                question="What is your pricing?",
                intent_decision=self._intent(Intent.PRICING_QUESTION),
            )

    def test_foreign_retrieval_result_is_rejected_even_if_retriever_regresses(self):
        self.org_a.settings = {"pricing": {}}
        self.org_a.save(update_fields=["settings"])
        foreign = SimpleNamespace(chunk=self.chunk_b, similarity=0.99)
        with patch(
            "apps.ai_engagement.services.evidence_resolver.KnowledgeRetrievalService.retrieve_by_keyword",
            return_value=[foreign],
        ):
            with self.assertRaises(TenantScopeError):
                EvidenceResolver().resolve(
                    organization=self.org_a,
                    lead=self.lead_a,
                    question="What is the refund policy?",
                    intent_decision=self._intent(Intent.POLICY_QUESTION),
                )

    def test_evidence_resolution_is_deterministic_and_does_not_call_provider(self):
        with patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text") as generate:
            EvidenceResolver().resolve(
                organization=self.org_b,
                lead=self.lead_b,
                question="price kya hai?",
                intent_decision=self._intent(Intent.PRICING_QUESTION),
            )
        generate.assert_not_called()

    def test_same_phone_memory_is_isolated_by_organization_and_lead(self):
        memory = StructuredLeadMemoryService()
        snapshot_a, _ = memory.merge_facts(
            organization=self.org_a,
            lead=self.lead_a,
            facts=[{
                "requirement_id": "volume",
                "value": 30,
                "confidence": 0.99,
                "evidence": "around 30 leads daily",
            }],
            source_message_id="msg-a",
        )
        snapshot_b, _ = memory.merge_facts(
            organization=self.org_b,
            lead=self.lead_b,
            facts=[{
                "requirement_id": "volume",
                "value": 5,
                "confidence": 0.99,
                "evidence": "around 5 leads daily",
            }],
            source_message_id="msg-b",
        )
        self.assertEqual(snapshot_a["facts"]["qualification.volume"]["value"], 30)
        self.assertEqual(snapshot_b["facts"]["qualification.volume"]["value"], 5)
        self.assertEqual(snapshot_a["organization_id"], str(self.org_a.id))
        self.assertEqual(snapshot_b["organization_id"], str(self.org_b.id))
        self.assertNotEqual(snapshot_a["lead_id"], snapshot_b["lead_id"])

    def test_lower_confidence_fact_cannot_overwrite_stronger_fact(self):
        memory = StructuredLeadMemoryService()
        memory.merge_facts(
            organization=self.org_a,
            lead=self.lead_a,
            facts=[{"key": "budget", "value": "₹50k", "confidence": 0.95}],
            source_message_id="strong",
        )
        snapshot, mutations = memory.merge_facts(
            organization=self.org_a,
            lead=self.lead_a,
            facts=[{"key": "budget", "value": "₹10k", "confidence": 0.40}],
            source_message_id="weak",
        )
        self.assertEqual(snapshot["facts"]["budget"]["value"], "₹50k")
        self.assertFalse(mutations[0].accepted)
        self.assertEqual(mutations[0].reason, "lower_confidence_conflict")

    def test_memory_provenance_and_trace_include_old_proposed_and_acceptance(self):
        memory = StructuredLeadMemoryService()
        _, first = memory.merge_facts(
            organization=self.org_a,
            lead=self.lead_a,
            facts=[{
                "key": "timeline",
                "value": "this month",
                "confidence": 0.8,
                "evidence": "We need it this month",
            }],
            source_message_id="m1",
        )
        snapshot, second = memory.merge_facts(
            organization=self.org_a,
            lead=self.lead_a,
            facts=[{
                "key": "timeline",
                "value": "next month",
                "confidence": 0.9,
                "evidence": "Actually next month",
            }],
            source_message_id="m2",
        )
        trace = memory.trace_payload(snapshot=snapshot, mutations=second)
        self.assertTrue(first[0].accepted)
        self.assertTrue(second[0].accepted)
        self.assertEqual(second[0].old["value"], "this month")
        self.assertEqual(second[0].proposed["value"], "next month")
        self.assertEqual(trace["mutations"][0]["old"]["value"], "this month")
        self.assertEqual(trace["mutations"][0]["proposed"]["value"], "next month")
        self.assertTrue(trace["mutations"][0]["accepted"])

    def test_copied_memory_payload_fails_closed_in_another_tenant(self):
        memory = StructuredLeadMemoryService()
        memory.merge_facts(
            organization=self.org_a,
            lead=self.lead_a,
            facts=[{"key": "private_fact", "value": "ORG_A_ONLY", "confidence": 1.0}],
            source_message_id="m-a",
        )
        self.lead_a.refresh_from_db()
        self.lead_b.refresh_from_db()
        attrs_b = dict(self.lead_b.attributes or {})
        attrs_b[MEMORY_KEY] = self.lead_a.attributes[MEMORY_KEY]
        self.lead_b.attributes = attrs_b
        self.lead_b.save(update_fields=["attributes"])
        with self.assertRaises(StructuredMemoryScopeError):
            memory.load(organization=self.org_b, lead=self.lead_b)

    def test_trace_grounding_contains_category_source_ids_and_scores_not_content(self):
        result = EvidenceResolver().resolve(
            organization=self.org_b,
            lead=self.lead_b,
            question="What is your price?",
            intent_decision=self._intent(Intent.PRICING_QUESTION),
        )
        trace = result.trace_dict()
        self.assertEqual(trace["category"], "STRUCTURED_ORG_DATA")
        self.assertTrue(trace["source_ids"])
        self.assertEqual(trace["sources"][0]["score"], 1.0)
        self.assertNotIn("content", trace["sources"][0])

    def test_no_evidence_grounding_short_circuits_second_model_call(self):
        resolution = EvidenceResolution(
            category=GroundingCategory.NO_VERIFIED_EVIDENCE,
            information_class=InformationClass.UNKNOWN,
            question_type="pricing",
            sensitive=True,
            verified=False,
            controlled_fallback="No verified pricing.",
        )
        token = phase5_6_runtime._ACTIVE_EVIDENCE.set({
            "organization_id": str(self.org_empty.id),
            "lead_id": str(self.lead_empty.id),
            "resolution": resolution,
        })
        try:
            decision = EngagementDecision(
                should_engage=True,
                message="Invented price ₹123",
                file_document_id=None,
                crm_actions=[],
                reason="ANSWER_ORG_QUESTION",
                reason_code="ANSWER_ORG_QUESTION",
                model="test",
            )
            state = {
                "decision": decision,
                "organization": self.org_empty,
                "lead": self.lead_empty,
            }
            with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
                result = check_grounding(state)
            provider.assert_not_called()
            self.assertFalse(result["grounding_approved"])
            self.assertEqual(result["decision"].reason_code, "UNKNOWN_INFORMATION")
            self.assertNotIn("₹123", result["decision"].message)
        finally:
            phase5_6_runtime._ACTIVE_EVIDENCE.reset(token)
