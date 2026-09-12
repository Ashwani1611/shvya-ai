import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.db import transaction
from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.graph.workflow import _generate
from apps.ai_engagement.services.engagement import EngagementDecision, EngagementService
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_state import state_for_lead
from apps.ai_engagement.services.runtime_state import STATE_KEY, state_revision
from apps.ai_engagement.tests import test_engagement_controls as controls
from apps.ai_engagement.tests import test_precise_orchestration as precise


class GenerationPolicyRegressionTests(SimpleTestCase):
    def test_graph_preserves_authoring_instead_of_recompiling_question_text(self):
        context = precise.PreciseEngagementTests()._context()
        context.organization['qualification_requirements'] = '[id: owner] Do you own a business?\nA. Yes\nB. No'
        before = deepcopy(context.organization)
        legacy = Mock()
        _generate({'context': context, 'requirements': [{'id': 'owner', 'question': 'Do you own a business?'}],
                   'service': Mock(), 'organization': Mock(), 'lead': Mock(), 'legacy_engage': legacy})
        self.assertEqual(legacy.call_args.kwargs['context'].organization, before)

    def test_audit_refresh_does_not_invalidate_response_but_answer_change_does(self):
        lead = SimpleNamespace(stage_id='new', attributes={'_shvya_ai_qualification': {
            'history': [], 'requirement_states': {'city': {'status': 'asked', 'updated_at': 'old'}}}})
        revision = state_revision(lead)
        state = lead.attributes['_shvya_ai_qualification']
        state['history'].append({'event': 'asked'})
        state['requirement_states']['city']['updated_at'] = 'new'
        self.assertEqual(revision, state_revision(lead))
        state['requirement_states']['city']['status'] = 'answered'
        self.assertNotEqual(revision, state_revision(lead))

    def test_exact_backend_question_is_verified_without_second_provider_call(self):
        from apps.ai_engagement.graph.evidence import check_grounding
        question = "What is your budget?"
        decision = EngagementDecision(should_engage=True, message=question, file_document_id=None,
            crm_actions=[], reason="QUALIFICATION_NEXT", model="test", next_requirement_id="budget")
        state = {'decision': decision, 'context': SimpleNamespace(lead={}, organization={}, knowledge=[]),
            'requirements': [{'id': 'budget', 'question': question}],
            'qualification_state': {'conversation_mode': 'qualifying', 'requirement_states': {}},
            'organization': SimpleNamespace(id='org'), 'lead': SimpleNamespace(id='lead')}
        with patch('apps.ai_engagement.graph.evidence.OpenAIProvider') as provider:
            self.assertTrue(check_grounding(state)['grounding_approved'])
            provider.assert_not_called()
            # Adding a claim must still invoke the independent verifier.
            from dataclasses import replace
            state['decision'] = replace(decision, message=question + ' Your appointment is confirmed.')
            provider.return_value.generate_text.return_value.text = '{"approved": false}'
            self.assertFalse(check_grounding(state)['grounding_approved'])
            provider.return_value.generate_text.assert_called_once()

    def test_changed_state_uses_a_new_generation_claim(self):
        context = precise.PreciseEngagementTests()._context('Hello')
        context.organization['qualification_requirements'] = ''
        context.conversation['messages'][0]['id'] = 'same-inbound'
        provider = Mock()
        provider.generate_text.return_value = AITextResult(json.dumps({
            'should_engage': True, 'message': 'Hello!', 'file_document_id': None,
            'crm_actions': [], 'next_requirement_id': None, 'reason_code': 'NORMAL_CONVERSATION'}), 'test')
        lead = SimpleNamespace(id='lead-1', attributes={})
        with patch('apps.ai_engagement.services.engagement.EngagementGenerationLock') as lock:
            lock.return_value.acquire.return_value = True
            service = EngagementService(provider=provider)
            for status in ['unknown', 'declined']:
                lead.attributes[STATE_KEY] = {'booking_status': status}
                service._langgraph_legacy_engage(organization=SimpleNamespace(id='org-1'), lead=lead, context=context)
        self.assertNotEqual(lock.call_args_list[0].kwargs['source_message_id'], lock.call_args_list[1].kwargs['source_message_id'])


class ChannelQualificationRegressionTests(TestCase):
    setUp = controls.AIEngagementControlTests.setUp
    _inbound = controls.AIEngagementControlTests._inbound

    def _advance_option(self, connection_type):
        from apps.ai_engagement.models import OrgInfo
        from apps.ai_engagement.tasks import _persist_engagement_answers
        from apps.channels.models import WhatsAppMessage
        from apps.crm.models import Lead
        source = '[id: owner] Do you own a business?\nA. Yes\nB. No\n[id: budget] What is your budget?'
        OrgInfo.objects.update_or_create(organization=self.organization, defaults={
            'qualification_requirements': source, 'about': 'Shvya Test business information',
            'engagement_instructions': 'Be concise and friendly', 'bot_languages': 'English'})
        requirements = compile_qualification_requirements(source)['requirements']
        self.account.connection_type = connection_type
        self.account.save(update_fields=['connection_type'])
        for index, answer in enumerate(['A', 'a', '1', 'option a', 'Yes']):
            with self.subTest(answer=answer, channel=connection_type):
                cache.clear()
                self.lead = Lead.objects.create(organization=self.organization, pipeline=self.pipeline,
                    stage=self.new_lead, name='Option test', phone=f'+91922222222{index}')
                # The same outbound signal is used by both API and hosted delivery.
                WhatsAppMessage.objects.create(organization=self.organization, account=self.account,
                    lead=self.lead, direction='outbound', status='sent', body=requirements[0]['question'],
                    raw_payload={'shvya_ai': {'reason': 'QUALIFICATION_NEXT', 'next_requirement_id': requirements[0]['id']}})
                self.lead.refresh_from_db()
                inbound = self._inbound(f'{connection_type}-{index}')
                inbound.body = answer
                inbound.save(update_fields=['body'])
                provider = Mock()
                provider.generate_text.return_value = AITextResult(json.dumps({
                    'should_engage': True, 'message': requirements[1]['question'], 'file_document_id': None,
                    'crm_actions': [], 'next_requirement_id': requirements[1]['id'],
                    'qualification_updates': [], 'reason_code': 'QUALIFICATION_NEXT'}), 'test')
                service = EngagementService(provider=provider)
                with patch('apps.ai_engagement.services.engagement.EngagementGenerationLock') as lock:
                    lock.return_value.acquire.return_value = True
                    decision = service.engage(organization=self.organization, lead=self.lead)
                with transaction.atomic():
                    locked = Lead.objects.select_for_update().get(pk=self.lead.pk)
                    self.assertTrue(_persist_engagement_answers(locked, decision, inbound.pk))
                    self.assertFalse(_persist_engagement_answers(locked, decision, inbound.pk))
                self.lead.refresh_from_db()
                state = state_for_lead(self.lead, requirements=requirements)
                self.assertEqual(state['requirement_states'][requirements[0]['id']]['status'], 'answered')
                self.assertEqual(decision.next_requirement_id, requirements[1]['id'])
                payload = json.loads(provider.generate_text.call_args.kwargs['input_text'])
                self.assertEqual(payload['organization']['about'], 'Shvya Test business information')
                self.assertEqual(payload['organization']['engagement_instructions'], 'Be concise and friendly')

    def test_api_options_advance_and_finalize_once(self):
        self._advance_option('api')

    def test_hosted_options_advance_and_finalize_once(self):
        self._advance_option('hosted')

    def test_pause_survives_answer_persistence_row_reload(self):
        from apps.ai_engagement.tasks import _persist_engagement_answers
        inbound = self._inbound()
        inbound.body = 'not now'
        inbound.save(update_fields=['body'])
        decision = EngagementDecision(should_engage=True, message='We can continue later.',
            file_document_id=None, crm_actions=[], reason='ACK', model='test')
        with transaction.atomic():
            self.assertTrue(_persist_engagement_answers(self.lead, decision, inbound.pk))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes[STATE_KEY]['conversation_mode'], 'paused')
