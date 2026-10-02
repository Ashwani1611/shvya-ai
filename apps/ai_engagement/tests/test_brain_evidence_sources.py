from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.models import Chunk, Document, FAQ, OrgInfo
from apps.ai_engagement.services.authored_knowledge import authored_answer_candidates, matching_authored_answers
from apps.ai_engagement.services.evidence_resolver import (
    EvidenceItem, EvidenceResolution, EvidenceResolver, GroundingCategory, InformationClass,
)
from apps.ai_engagement.services.intent_types import Intent, IntentDecision
from apps.ai_engagement.services import phase5_6_runtime as runtime
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class BrainEvidenceSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Brain sources")
        cls.other = Organization.objects.create(name="Other tenant")
        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization, name="Sales",
            country_code="+91", phone_number="9000000461",
        )
        cls.lead = Lead.objects.create(
            organization=cls.organization, pipeline=cls.pipeline,
            stage=cls.pipeline.stages.get(name="New leads"),
            phone="+919000000462", name="Customer",
        )
        cls.info, _ = OrgInfo.objects.get_or_create(organization=cls.organization)

    def resolve(self, question, intent):
        return EvidenceResolver().resolve(
            organization=self.organization, lead=self.lead, question=question,
            intent_decision=IntentDecision(primary_intent=intent, requires_knowledge=True),
        )

    def chunk(self, content, **kwargs):
        owner = kwargs.pop("organization", self.organization)
        document = Document.objects.create(
            organization=owner, name="Source",
            processing_status=kwargs.pop("processing_status", "completed"), **kwargs,
        )
        return Chunk.objects.create(organization=owner, document=document, content=content)

    def refine(self, resolution, knowledge):
        context = SimpleNamespace(
            organization={"id": str(self.organization.pk)},
            lead={"id": str(self.lead.pk)}, knowledge=knowledge,
        )
        token = runtime._ACTIVE_EVIDENCE.set({
            "organization_id": str(self.organization.pk),
            "lead_id": str(self.lead.pk), "resolution": resolution,
        })
        try:
            with patch(
                "apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text",
                side_effect=AssertionError("Refinement must reuse existing retrieval"),
            ):
                return runtime.refine_evidence_from_context(context=context, resolution=resolution)
        finally:
            runtime._ACTIVE_EVIDENCE.reset(token)

    def test_about_price_is_available_to_sensitive_grounding(self):
        self.info.about = "We provide sales automation.\n\nDIY costs ₹2999 per seat per month."
        self.info.ai_playbook = "## Rules\nPrivate approval code is PRIVATE_RULE_VALUE."
        self.info.save(update_fields=["about", "ai_playbook"])
        result = self.resolve("How much does DIY cost?", Intent.PRICING_QUESTION)
        self.assertTrue(result.verified)
        self.assertTrue(result.sensitive)
        self.assertIn("₹2999", str(result.prompt_dict()))
        self.assertNotIn("PRIVATE_RULE_VALUE", str(result.prompt_dict()))
        self.assertEqual(result.evidence[0].metadata["field"], "about")

    def test_about_policy_keeps_conditions_in_same_paragraph(self):
        self.info.about = "Refunds are available. Only within 7 days and before activation."
        self.info.save(update_fields=["about"])
        result = self.resolve("What is your refund policy?", Intent.POLICY_QUESTION)
        self.assertTrue(result.verified)
        self.assertIn(self.info.about, [item.content for item in result.evidence])

    def test_unrelated_about_does_not_supply_a_missing_sensitive_fact(self):
        self.info.about = "We help businesses manage customer conversations."
        self.info.save(update_fields=["about"])
        result = self.resolve("What is your refund policy?", Intent.POLICY_QUESTION)
        self.assertFalse(result.verified)
        self.assertEqual(result.category, GroundingCategory.NO_VERIFIED_EVIDENCE)

    def test_semantic_policy_answer_supplements_general_structured_policy(self):
        existing = EvidenceItem(
            source_id="organization_profile:business_facts:policies",
            source_type="organization_runtime_profile", content="We protect customer data.",
        )
        resolution = EvidenceResolution(
            category=GroundingCategory.STRUCTURED_ORG_DATA,
            information_class=InformationClass.STATIC_CONFIGURED,
            question_type="policy", sensitive=True, verified=True, evidence=(existing,),
        )
        chunk = self.chunk("Annual subscriptions can be cancelled within seven days.")
        result = self.refine(resolution, [
            {"chunk_id": chunk.pk, "similarity": 0.91, "content": "Injected content"},
        ])
        self.assertEqual(result.category, GroundingCategory.STRUCTURED_ORG_DATA)
        self.assertEqual(result.evidence[0], existing)
        self.assertIn(chunk.content, [item.content for item in result.evidence])
        self.assertNotIn("Injected content", str(result.prompt_dict()))

    def test_repeated_refinement_deduplicates_and_bounds_context(self):
        resolution = EvidenceResolution(
            category=GroundingCategory.KNOWLEDGE_BASE,
            information_class=InformationClass.DYNAMIC_RETRIEVED,
            question_type="pricing", sensitive=True, verified=True,
            evidence=tuple(
                EvidenceItem(source_id=f"configured:{index}", source_type="organization_runtime_profile",
                             content=f"Configured {index}: " + "x" * 3900)
                for index in range(4)
            ),
        )
        chunks = [self.chunk(f"Source {index}: " + "y" * 3900) for index in range(4)]
        knowledge = [{"chunk_id": chunk.pk, "similarity": 0.9} for chunk in chunks]
        first = self.refine(resolution, knowledge + knowledge)
        second = self.refine(first, knowledge + knowledge)
        for result in (first, second):
            ids = [item.source_id for item in result.evidence]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertLessEqual(sum(len(item.content) for item in result.evidence), 12000)
            self.assertLessEqual(len(result.evidence), 8)
            self.assertTrue(any(item.metadata.get("chunk_id") == chunks[0].pk for item in result.evidence))

    def test_foreign_failed_and_blank_semantic_hits_cannot_verify(self):
        foreign = self.chunk("Foreign refund terms.", organization=self.other)
        failed = self.chunk("Unprocessed refund terms.", processing_status="failed")
        blank = self.chunk("   ")
        resolution = EvidenceResolution(
            category=GroundingCategory.NO_VERIFIED_EVIDENCE,
            information_class=InformationClass.UNKNOWN,
            question_type="policy", sensitive=True, verified=False,
        )
        result = self.refine(resolution, [
            {"chunk_id": chunk.pk, "similarity": 1.0} for chunk in (foreign, failed, blank)
        ])
        self.assertFalse(result.verified)
        self.assertFalse(result.evidence)

    def test_semantic_refinement_keeps_complete_faq_conditions(self):
        content = "Question: Refund policy?\nAnswer: " + "Policy details. " * 350 + "No refunds after activation."
        faq = EvidenceItem(
            source_id="faq:long", source_type="organization_faq", content=content,
            metadata={"requires_relevance_verification": True},
        )
        resolution = EvidenceResolution(
            category=GroundingCategory.STRUCTURED_ORG_DATA,
            information_class=InformationClass.STATIC_CONFIGURED,
            question_type="policy", sensitive=True, verified=True, evidence=(faq,),
        )
        chunk = self.chunk("Contact support for cancellation requests.")
        result = self.refine(resolution, [{"chunk_id": chunk.pk, "similarity": 0.9}])
        retained = next(item for item in result.evidence if item.source_id == faq.source_id)
        self.assertEqual(retained.content, content)
        self.assertTrue(retained.content.endswith("No refunds after activation."))

    def test_static_about_cannot_confirm_a_live_appointment(self):
        self.info.about = "Our office is open Monday to Friday, 9 AM to 6 PM."
        self.info.save(update_fields=["about"])
        result = self.resolve("Is an appointment available tomorrow at 3 PM?", Intent.AVAILABILITY_QUESTION)
        self.assertFalse(result.verified)
        self.assertEqual(result.question_type, "appointment_availability")

    def test_static_service_availability_uses_authored_knowledge(self):
        self.info.about = "Our cybersecurity course is available online in English and Hindi."
        self.info.save(update_fields=["about"])
        result = self.resolve("Is your cybersecurity course available online?", Intent.AVAILABILITY_QUESTION)
        self.assertTrue(result.verified)
        self.assertEqual(result.question_type, "availability")
        self.assertIn("English and Hindi", str(result.prompt_dict()))

    def test_open_slot_still_requires_live_availability(self):
        self.info.about = "Appointments are available during office hours."
        self.info.save(update_fields=["about"])
        result = self.resolve("Is there an open appointment slot tomorrow?", Intent.AVAILABILITY_QUESTION)
        self.assertFalse(result.verified)
        self.assertEqual(result.question_type, "appointment_availability")

    def test_relevant_faq_after_first_hundred_is_ranked_before_limit(self):
        FAQ.objects.bulk_create([
            FAQ(organization=self.organization, question=f"Generic question {index}",
                answer="Contact support for account help.")
            for index in range(105)
        ])
        target = FAQ.objects.create(
            organization=self.organization, question="What is your refund policy?",
            answer="Refunds are available within seven days.",
        )
        answers = matching_authored_answers(
            organization=self.organization, question="What is your refund policy?",
        )
        self.assertEqual([item["source_id"] for item in answers], [f"faq:{target.pk}"])

    def test_public_answer_topic_matches_without_private_rules_or_foreign_faq(self):
        target = FAQ.objects.create(
            organization=self.organization, question="Which courses do you offer?",
            answer="We provide CompTIA Security+ and CCNA training.",
        )
        FAQ.objects.create(
            organization=self.other, question="Security+", answer="Private foreign answer.",
        )
        FAQ.objects.create(
            organization=self.organization, question="Security+", answer="Retired answer.", is_active=False,
        )
        self.info.ai_playbook = "## Rules\nSecurity+ private escalation instructions."
        self.info.save(update_fields=["ai_playbook"])
        answers = matching_authored_answers(organization=self.organization, question="Security+")
        self.assertEqual([item["source_id"] for item in answers], [f"faq:{target.pk}"])
        self.assertNotIn("private escalation", str(answers))

    def test_multilingual_faq_question_surfaces_complete_authored_candidates(self):
        answer = 'Refunds are available within seven days, only before course activation.'
        faq = FAQ.objects.create(organization=self.organization,
            question='What is your refund policy?', answer=answer)
        FAQ.objects.create(organization=self.other,
            question='What is your refund policy?', answer='Foreign private terms.')
        FAQ.objects.create(organization=self.organization, is_active=False,
            question='What is your refund policy?', answer='Inactive terms.')
        self.info.ai_playbook = '## Rules\nDo not reveal PRIVATE_RULE_VALUE.'
        self.info.save(update_fields=['ai_playbook'])
        result = self.resolve('पैसे वापस लेने की शर्तें क्या हैं?', Intent.POLICY_QUESTION)
        self.assertTrue(result.verified)
        self.assertTrue(result.sensitive)
        self.assertEqual([item.source_id for item in result.evidence], [f'faq:{faq.pk}'])
        self.assertEqual(result.evidence[0].content, f'Question: {faq.question}\nAnswer: {answer}')
        self.assertTrue(result.evidence[0].metadata['requires_relevance_verification'])
        self.assertNotIn('PRIVATE_RULE_VALUE', str(result.prompt_dict()))

    def test_unrelated_authored_candidate_cannot_skip_sensitive_relevance_verification(self):
        from apps.ai_engagement.services.grounding_safety import exact_evidence_reply
        FAQ.objects.create(organization=self.organization,
            question='Which subjects do you teach?', answer='We provide CCNA training.')
        result = self.resolve('What is your refund policy?', Intent.POLICY_QUESTION)
        self.assertTrue(result.evidence)
        # Even an exact copy of the complete approved source is not a relevant
        # answer to the refund question and must reach the independent verifier.
        decision = SimpleNamespace(message=result.evidence[0].content,
            crm_actions=[], qualification_updates=[], file_document_id=None)
        self.assertFalse(exact_evidence_reply(decision, result))

    def test_unrelated_faq_reply_reaches_independent_guard_and_is_rejected(self):
        from apps.ai_engagement.graph.evidence import check_grounding
        from apps.ai_engagement.services.engagement import EngagementDecision
        FAQ.objects.create(organization=self.organization,
            question='Which subjects do you teach?', answer='We provide CCNA training.')
        question = 'How much does the course cost?'
        resolution = self.resolve(question, Intent.PRICING_QUESTION)
        context = SimpleNamespace(organization={}, knowledge=[], lead={'attributes': {}},
            conversation={'messages': []}, stage={})
        decision = EngagementDecision(should_engage=True,
            message=resolution.evidence[0].content, file_document_id=None,
            crm_actions=[], reason='ANSWER_ORG_QUESTION', reason_code='ANSWER_ORG_QUESTION', model='test')
        token = runtime._ACTIVE_EVIDENCE.set({
            'organization_id': str(self.organization.pk), 'lead_id': str(self.lead.pk),
            'resolution': resolution,
        })
        try:
            with patch('apps.ai_engagement.graph.evidence.OpenAIProvider') as provider:
                provider.return_value.generate_text.return_value.text = (
                    '{"approved":false,"reason":"unsupported_claim"}'
                )
                result = check_grounding({'decision': decision, 'context': context,
                    'organization': self.organization, 'lead': self.lead, 'latest_text': question,
                    'requirements': [], 'qualification_state': {}})
            provider.return_value.generate_text.assert_called_once()
            self.assertFalse(result['grounding_approved'])
            self.assertEqual(result['decision'].reason_code, 'UNKNOWN_INFORMATION')
        finally:
            runtime._ACTIVE_EVIDENCE.reset(token)

    def test_authored_candidates_keep_later_pairs_and_never_truncate_conditions(self):
        for index in range(6):
            FAQ.objects.create(organization=self.organization,
                question=f'Course subject {index}?', answer=f'Subject {index} is included.')
        target = FAQ.objects.create(organization=self.organization,
            question='Refund eligibility?', answer='Only within seven days and before activation.')
        candidates = authored_answer_candidates(organization=self.organization,
            question='refund eligibility', max_chars=300)
        self.assertEqual(candidates[0]['source_id'], f'faq:{target.pk}')
        self.assertLessEqual(sum(len(item['content']) for item in candidates), 300)
        self.assertIn('and before activation.', candidates[0]['content'])
        self.assertEqual(authored_answer_candidates(organization=self.organization,
            question='refund eligibility', max_chars=20), [])

    def test_faq_fallback_does_not_authorize_live_slots(self):
        FAQ.objects.create(organization=self.organization,
            question='Which subjects do you teach?', answer='We provide CCNA training.')
        result = self.resolve('Is an appointment available tomorrow?', Intent.AVAILABILITY_QUESTION)
        self.assertFalse(result.verified)
        self.assertEqual(result.evidence, ())
