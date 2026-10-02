from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import Chunk, Document, FAQ, OrgInfo
from apps.ai_engagement.services.authored_knowledge import faq_pairs, matching_authored_answers
from apps.ai_engagement.services.evidence_resolver import EvidenceResolver
from apps.ai_engagement.services.intent_types import Intent, IntentDecision
from apps.ai_engagement.services.response_composer import build_response_plan
from apps.ai_engagement.services.retrieval import KnowledgeRetrievalService
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class AuthoredFAQFormatTests(SimpleTestCase):
    def test_markdown_labels_and_private_note_continuations(self):
        for question, answer in (
            ('**Q:**', '**A:**'), ('**Question:**', '**Answer:**'),
            ('1. **Question 1:**', '- **Answer:**'), ('__Q:__', '__A:__'),
        ):
            for note in ('**Notes:**', '__Internal notes:__', 'Private notes:', 'Notes'):
                with self.subTest(question=question, note=note):
                    raw = (f'## FAQ\n{question} Is onboarding included?\n'
                           f'{answer} Onboarding is included.\n{note}\n'
                           'Private escalation token.\nA: This is a private answer draft.\n'
                           'Q: Do you offer training?\nA: Training is available.\n'
                           '## Rules\nQ: Private operating rule?\nA: Private CRM action.')
                    self.assertEqual(faq_pairs(raw), [
                        ('Is onboarding included?', 'Onboarding is included.'),
                        ('Do you offer training?', 'Training is available.'),
                    ])


class KnowledgeRuntimeReliabilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name='Knowledge Org')
        cls.other = Organization.objects.create(name='Other Org')
        cls.pipeline = Pipeline.objects.create(organization=cls.organization, name='Sales',
            country_code='+91', phone_number='9000000311')
        cls.lead = Lead.objects.create(organization=cls.organization, pipeline=cls.pipeline,
            stage=cls.pipeline.stages.get(name='New leads'), phone='+919000000312', name='Priya')
        cls.info, _ = OrgInfo.objects.get_or_create(organization=cls.organization)

    def source(self, *, content, name='Guide', organization=None, **kwargs):
        owner = organization or self.organization
        doc = Document.objects.create(organization=owner, name=name,
            processing_status=kwargs.pop('processing_status', 'completed'), **kwargs)
        return Chunk.objects.create(organization=owner, document=doc, content=content)

    def resolve(self, question, intent=Intent.PRODUCT_OR_SERVICE_QUESTION, **kwargs):
        return EvidenceResolver().resolve(organization=self.organization, lead=self.lead,
            question=question, intent_decision=IntentDecision(primary_intent=intent, **kwargs))

    def test_new_relevant_website_survives_candidate_bound_after_older_partial_hits(self):
        doc = Document.objects.create(organization=self.organization, name='Older guide',
            processing_status='completed')
        Chunk.objects.bulk_create([Chunk(organization=self.organization, document=doc,
            chunk_index=index, content='The onboarding team can help you with an account.')
            for index in range(65)])
        target = self.source(name='Implementation', source_url='https://example.com/implementation',
            content='Onboarding migration training includes a dedicated workshop.')
        results = KnowledgeRetrievalService().retrieve_by_keyword(organization=self.organization,
            query_text='Can you tell me about onboarding migration training?', limit=4)
        self.assertEqual([item.chunk.pk for item in results], [target.pk])

    def test_filler_words_and_partial_substrings_cannot_verify_missing_policy(self):
        self.source(content='The platform is useful. Your account has an explanation.', name='Manual')
        for question in ('What is your refund policy?', 'What is your plan?', 'Can you tell me more?'):
            with self.subTest(question=question):
                self.assertEqual(KnowledgeRetrievalService().retrieve_by_keyword(
                    organization=self.organization, query_text=question), [])
        self.assertFalse(self.resolve('What is your refund policy?', Intent.POLICY_QUESTION).verified)

    def test_website_and_file_evidence_exclude_foreign_and_unready_sources(self):
        target = self.source(content='Onboarding migration training includes a workshop.',
            file='knowledge/onboarding.pdf')
        for extra in ({'organization': self.other}, {'is_active': False},
                      {'processing_status': 'failed'}, {'processing_status': 'processing'}):
            self.source(content='Onboarding migration training: private or obsolete.', **extra)
        results = KnowledgeRetrievalService().retrieve_by_keyword(organization=self.organization,
            query_text='onboarding migration training')
        self.assertEqual([item.chunk.pk for item in results], [target.pk])

    def test_general_policy_setting_does_not_hide_exact_playbook_faq(self):
        self.organization.settings = {'policies': {'privacy': 'We protect customer data.'}}
        self.organization.save(update_fields=['settings'])
        self.info.ai_playbook = ('## FAQ\n**Q:** What is your refund policy?\n'
            '**A:** Refunds are available within 7 days.\n**Notes:** Never reveal internal approval codes.')
        self.info.save(update_fields=['ai_playbook'])
        resolution = self.resolve('What is your refund policy?', Intent.POLICY_QUESTION)
        self.assertTrue(resolution.verified)
        self.assertEqual(resolution.evidence[0].source_type, 'organization_runtime_profile')
        self.assertTrue(any(item.source_type == 'playbook_faq' and '7 days' in item.content
                            for item in resolution.evidence))
        self.assertNotIn('approval codes', str(resolution.prompt_dict()))

    def test_knowledge_required_followup_resolves_saved_file(self):
        target = self.source(content='Onboarding migration training includes a workshop.',
            file='knowledge/onboarding.pdf')
        resolution = self.resolve('Tell me about onboarding migration training',
            Intent.FOLLOW_UP_RESPONSE, requires_knowledge=True)
        self.assertTrue(resolution.verified)
        self.assertEqual(resolution.evidence[0].metadata['chunk_id'], target.pk)

    def test_short_faq_topic_query_matches_without_unrelated_or_foreign_answers(self):
        own = FAQ.objects.create(organization=self.organization,
            question='What is your refund policy?', answer='Refunds are available within 7 days.')
        FAQ.objects.create(organization=self.organization,
            question='Do you offer onboarding?', answer='Onboarding is included.')
        FAQ.objects.create(organization=self.other,
            question='What is your refund policy?', answer='Foreign terms.')
        answers = matching_authored_answers(organization=self.organization, question='refund')
        self.assertEqual([item['source_id'] for item in answers], [f'faq:{own.pk}'])

    def test_topic_later_in_authored_faq_answer_remains_retrievable(self):
        introduction = (
            'Our academy provides practical education across multiple technology domains '
            'through experienced instructors using hands-on projects and instructor-led '
            'workshops covering foundational skills alongside advanced professional topics. '
        )
        own = FAQ.objects.create(organization=self.organization,
            question='Which training subjects do you offer?',
            answer=introduction + 'We also provide CompTIA Security+ certification training.')
        FAQ.objects.create(organization=self.other, question='Security+', answer='Foreign terms.')
        answers = matching_authored_answers(organization=self.organization, question='Security+')
        self.assertEqual([item['source_id'] for item in answers], [f'faq:{own.pk}'])

        self.info.ai_playbook = (
            '## FAQ\nQ: Which certification tracks can I study?\nA: '
            + introduction + 'Our networking track includes CCNA preparation.'
        )
        self.info.save(update_fields=['ai_playbook'])
        answers = matching_authored_answers(organization=self.organization, question='CCNA')
        self.assertEqual([item['source_type'] for item in answers], ['playbook_faq'])
        self.assertIn('CCNA preparation', answers[0]['content'])

    def test_composer_keeps_verified_faq_with_saved_languages_on_later_stage(self):
        self.info.ai_playbook = '## FAQ\n**Q:** Is onboarding included?\n**A:** Onboarding is included.'
        self.info.save(update_fields=['ai_playbook'])
        intent = IntentDecision(primary_intent=Intent.PRODUCT_OR_SERVICE_QUESTION, language='Hindi')
        resolution = EvidenceResolver().resolve(organization=self.organization, lead=self.lead,
            question='Is onboarding included?', intent_decision=intent)
        payload = {'organization': {'id': str(self.organization.pk), 'about': 'We automate sales.',
                                   'bot_languages': 'English, Hindi', 'ai_playbook': self.info.ai_playbook},
                   'lead': {'id': str(self.lead.pk), 'qualification': {'engagement_mode': 'conversation'}},
                   'grounding': resolution.prompt_dict()}
        plan = build_response_plan(payload=payload, organization_id=self.organization.pk,
            lead_id=self.lead.pk, intent_decision=intent, final_composition=True)
        self.assertEqual(plan.allowed_languages, ('English', 'Hindi'))
        self.assertEqual(plan.language, 'Hindi')
        self.assertIsNone(plan.next_question)
        self.assertTrue(any(item['content'] == 'Onboarding is included.' for item in plan.allowed_facts))
        self.assertTrue(any(item['content'] == 'We automate sales.' for item in plan.allowed_facts))
