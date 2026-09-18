from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.graph.evidence import check_grounding
from apps.ai_engagement.services import phase5_6_runtime
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.evidence_resolver import (
    EvidenceItem,
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
from apps.ai_engagement.services.structured_memory import (
    MEMORY_KEY,
    StructuredLeadMemoryService,
)
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.organizations.models import Organization


class Phase56AuditFixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Phase 5/6 Audit Org",
            settings={
                "working_hours": "09:00-18:00",
                "availability": "Services are offered Monday to Saturday.",
                "pricing": {"pro": "₹999/month"},
            },
        )
        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Sales",
            country_code="+91",
            phone_number="9000000099",
        )
        cls.lead = Lead.objects.create(
            organization=cls.organization,
            pipeline=cls.pipeline,
            stage=cls.pipeline.stages.get(name="New leads"),
            name="Audit Lead",
            phone="+919999999991",
        )

    @staticmethod
    def _intent(primary: Intent, *, question: str):
        return IntentDecision(
            primary_intent=primary,
            confidence=0.99,
            direct_question=question,
            classification_path=ClassificationPath.DETERMINISTIC,
            requires_knowledge=True,
        )

    def test_specific_appointment_availability_requires_live_evidence(self):
        question = "Do you have an appointment slot tomorrow at 3 PM?"
        result = EvidenceResolver().resolve(
            organization=self.organization,
            lead=self.lead,
            question=question,
            intent_decision=self._intent(
                Intent.AVAILABILITY_QUESTION,
                question=question,
            ),
        )

        self.assertEqual(
            result.category,
            GroundingCategory.NO_VERIFIED_EVIDENCE,
        )
        self.assertEqual(result.question_type, "appointment_availability")
        self.assertTrue(result.sensitive)
        self.assertFalse(result.verified)
        self.assertIn("live appointment availability", result.controlled_fallback)
        self.assertNotIn("09:00-18:00", str(result.prompt_dict()))

    def test_working_hours_question_can_use_configured_hours(self):
        question = "What are your working hours?"
        result = EvidenceResolver().resolve(
            organization=self.organization,
            lead=self.lead,
            question=question,
            intent_decision=self._intent(
                Intent.AVAILABILITY_QUESTION,
                question=question,
            ),
        )

        self.assertEqual(result.category, GroundingCategory.STRUCTURED_ORG_DATA)
        self.assertTrue(result.verified)
        self.assertIn("09:00-18:00", str(result.prompt_dict()))

    def test_crm_attribute_remains_canonical_over_memory_inference(self):
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Budget",
            key="budget",
            field_type=AttributeDefinition.FieldType.NUMERIC,
        )
        self.lead.attributes = {"budget": 50000}
        self.lead.save(update_fields=["attributes"])

        memory = StructuredLeadMemoryService()
        snapshot, mutations = memory.merge_facts(
            organization=self.organization,
            lead=self.lead,
            facts=[
                {
                    "key": "budget",
                    "value": 20000,
                    "confidence": 0.99,
                    "source_type": "model_inference",
                    "evidence": "Maybe the budget is 20k",
                }
            ],
            source_message_id="budget-guess",
        )

        self.assertEqual(snapshot["facts"]["budget"]["value"], 50000)
        self.assertEqual(
            snapshot["facts"]["budget"]["source_type"],
            "verified_crm",
        )
        self.assertFalse(mutations[0].accepted)
        self.assertEqual(
            mutations[0].reason,
            "canonical_backend_truth_conflict",
        )

        self.lead.refresh_from_db()
        stored = (self.lead.attributes.get(MEMORY_KEY) or {}).get("facts") or {}
        self.assertNotIn("budget", stored)

    def test_higher_authority_customer_fact_beats_model_inference(self):
        memory = StructuredLeadMemoryService()
        memory.merge_facts(
            organization=self.organization,
            lead=self.lead,
            facts=[
                {
                    "key": "timeline",
                    "value": "next quarter",
                    "confidence": 0.99,
                    "source_type": "model_inference",
                }
            ],
            source_message_id="inference-1",
        )
        snapshot, mutations = memory.merge_facts(
            organization=self.organization,
            lead=self.lead,
            facts=[
                {
                    "key": "timeline",
                    "value": "this month",
                    "confidence": 0.80,
                    "source_type": "explicit_customer",
                    "evidence": "We need it this month",
                }
            ],
            source_message_id="customer-1",
        )

        self.assertEqual(snapshot["facts"]["timeline"]["value"], "this month")
        self.assertTrue(mutations[0].accepted)
        self.assertEqual(mutations[0].reason, "higher_authority_correction")

    def test_lower_authority_inference_cannot_overwrite_customer_fact(self):
        memory = StructuredLeadMemoryService()
        memory.merge_facts(
            organization=self.organization,
            lead=self.lead,
            facts=[
                {
                    "key": "preferred_contact_time",
                    "value": "evening",
                    "confidence": 0.80,
                    "source_type": "explicit_customer",
                }
            ],
            source_message_id="customer-2",
        )
        snapshot, mutations = memory.merge_facts(
            organization=self.organization,
            lead=self.lead,
            facts=[
                {
                    "key": "preferred_contact_time",
                    "value": "morning",
                    "confidence": 0.99,
                    "source_type": "model_inference",
                }
            ],
            source_message_id="inference-2",
        )

        self.assertEqual(
            snapshot["facts"]["preferred_contact_time"]["value"],
            "evening",
        )
        self.assertFalse(mutations[0].accepted)
        self.assertEqual(mutations[0].reason, "lower_authority_conflict")

    def test_low_risk_normal_reply_skips_grounding_model_call(self):
        resolution = EvidenceResolution(
            category=GroundingCategory.NO_VERIFIED_EVIDENCE,
            information_class=InformationClass.UNKNOWN,
            question_type="not_evidence_bound",
            sensitive=False,
            verified=False,
        )
        token = phase5_6_runtime._ACTIVE_EVIDENCE.set(
            {
                "organization_id": str(self.organization.id),
                "lead_id": str(self.lead.id),
                "resolution": resolution,
            }
        )
        try:
            decision = EngagementDecision(
                should_engage=True,
                message="Thanks for sharing that.",
                file_document_id=None,
                crm_actions=[],
                qualification_updates=[],
                reason="NORMAL_CONVERSATION",
                reason_code="NORMAL_CONVERSATION",
                model="test",
            )
            state = {
                "decision": decision,
                "organization": self.organization,
                "lead": self.lead,
            }
            with patch(
                "apps.ai_engagement.graph.evidence.OpenAIProvider"
            ) as provider:
                result = check_grounding(state)
            provider.assert_not_called()
            self.assertTrue(result["grounding_approved"])
            self.assertEqual(
                result["grounding_validation_path"],
                "deterministic_low_risk",
            )
        finally:
            phase5_6_runtime._ACTIVE_EVIDENCE.reset(token)

    def test_extractive_verified_pricing_skips_second_model_call(self):
        resolution = EvidenceResolution(
            category=GroundingCategory.STRUCTURED_ORG_DATA,
            information_class=InformationClass.STATIC_CONFIGURED,
            question_type="pricing",
            sensitive=True,
            verified=True,
            evidence=(
                EvidenceItem(
                    source_id="organization_profile:business_facts:pricing",
                    source_type="organization_runtime_profile",
                    content="₹999/month",
                ),
            ),
        )
        token = phase5_6_runtime._ACTIVE_EVIDENCE.set(
            {
                "organization_id": str(self.organization.id),
                "lead_id": str(self.lead.id),
                "resolution": resolution,
            }
        )
        try:
            decision = EngagementDecision(
                should_engage=True,
                message="Our price is ₹999/month.",
                file_document_id=None,
                crm_actions=[],
                qualification_updates=[],
                reason="ANSWER_ORG_QUESTION",
                reason_code="ANSWER_ORG_QUESTION",
                model="test",
            )
            state = {
                "decision": decision,
                "organization": self.organization,
                "lead": self.lead,
            }
            with patch(
                "apps.ai_engagement.graph.evidence.OpenAIProvider"
            ) as provider:
                result = check_grounding(state)
            provider.assert_not_called()
            self.assertTrue(result["grounding_approved"])
            self.assertEqual(
                result["grounding_validation_path"],
                "deterministic_evidence_match",
            )
        finally:
            phase5_6_runtime._ACTIVE_EVIDENCE.reset(token)

    def test_unsupported_pricing_claim_still_uses_grounding_validator(self):
        resolution = EvidenceResolution(
            category=GroundingCategory.STRUCTURED_ORG_DATA,
            information_class=InformationClass.STATIC_CONFIGURED,
            question_type="pricing",
            sensitive=True,
            verified=True,
            evidence=(
                EvidenceItem(
                    source_id="organization_profile:business_facts:pricing",
                    source_type="organization_runtime_profile",
                    content="₹999/month",
                ),
            ),
        )
        token = phase5_6_runtime._ACTIVE_EVIDENCE.set(
            {
                "organization_id": str(self.organization.id),
                "lead_id": str(self.lead.id),
                "resolution": resolution,
            }
        )
        try:
            decision = EngagementDecision(
                should_engage=True,
                message="Our price is ₹1999/month.",
                file_document_id=None,
                crm_actions=[],
                qualification_updates=[],
                reason="ANSWER_ORG_QUESTION",
                reason_code="ANSWER_ORG_QUESTION",
                model="test",
            )
            context = SimpleNamespace(
                conversation={"messages": []},
                organization={
                    "about": "",
                    "name": self.organization.name,
                    "engagement_instructions": "",
                    "bot_languages": "",
                },
                knowledge=[],
                lead={"attributes": {}},
            )
            state = {
                "decision": decision,
                "organization": self.organization,
                "lead": self.lead,
                "context": context,
                "latest_text": "What is your price?",
                "runtime_policy": {},
                "qualification_state": {},
                "requirements": [],
            }
            with patch(
                "apps.ai_engagement.graph.evidence.OpenAIProvider"
            ) as provider_cls:
                provider_cls.return_value.generate_text.return_value = SimpleNamespace(
                    text='{"approved": false, "reason": "unsupported_price"}'
                )
                result = check_grounding(state)
            provider_cls.assert_called_once()
            self.assertFalse(result["grounding_approved"])
            self.assertEqual(result["decision"].reason_code, "UNKNOWN_INFORMATION")
            self.assertNotIn("₹1999", result["decision"].message)
        finally:
            phase5_6_runtime._ACTIVE_EVIDENCE.reset(token)
