from tests.playbook_fixtures import build_ai_playbook
from tests.brain_fixtures import mocked_brain_profile_loader

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playground import PlaygroundService
from apps.ai_engagement.services.runtime_state import contract, validate_response
from apps.ai_engagement.tests import test_engagement_controls as controls


class AuthoredQualificationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.raw = ('What is your biggest challenge? A) Slow replies B) Missed follow-ups?\n'
                    'Where do you manage leads? A) WhatsApp B) Excel?\n'
                    'Do you currently run ads? A) Yes B) No?\n'
                    'If the lead does not run ads, where do leads come from? A) Referrals B) Other?')
        self.requirements = compile_qualification_requirements(self.raw)['requirements']

    def test_formatting_does_not_reject_correct_options(self):
        req = self.requirements[0]
        runtime = contract(qualification={'conversation_mode': 'qualifying'}, requirements=self.requirements)
        validate_response(decision=SimpleNamespace(next_requirement_id=req['id'], should_engage=True,
            message='What is your biggest challenge? A. slow replies B. missed follow-ups'),
            runtime=runtime, requirements=self.requirements)
        with self.assertRaises(ValueError):
            validate_response(decision=SimpleNamespace(next_requirement_id=req['id'], should_engage=True,
                message='Missed follow-ups / Slow replies'), runtime=runtime, requirements=self.requirements)

    def test_leading_ads_condition_is_compiled(self):
        condition = self.requirements[-1]['eligible_when']
        self.assertEqual(condition, {'requirement_id': self.requirements[-2]['id'], 'operator': 'eq', 'value': False})

    def _sandbox(self, runs_ads):
        info = Mock()
        info.get_or_create.return_value = SimpleNamespace(ai_enabled=True, about='Lead management service',
            bot_languages='English', ai_playbook=build_ai_playbook(questions=self.raw, rules='Ask one question at a time.'),
            bump_up_enabled=False, bump_up_count=0)
        provider = Mock()
        question_indices = [0, 1, 2] + ([] if runs_ads else [3]) + [None]
        outputs = []
        for index in question_indices:
            req = self.requirements[index] if index is not None else None
            outputs.append(AITextResult(json.dumps({'should_engage': True,
                'message': req['question'] if req else 'Thanks, your answers are complete.',
                'file_document_id': None, 'crm_actions': [], 'qualification_updates': [],
                'next_requirement_id': req['id'] if req else None,
                'reason_code': 'QUALIFICATION_NEXT' if req else 'NORMAL_CONVERSATION'}), 'test'))
        provider.generate_text.side_effect = outputs
        retrieval = Mock()
        retrieval.retrieve_by_vector.return_value = []
        service = PlaygroundService(provider=provider, org_info_service=info,
            embedding_service=Mock(), retrieval_service=retrieval,
            runtime_profile_loader=mocked_brain_profile_loader(info, requirements=self.requirements))
        organization = SimpleNamespace(id='sandbox-regression', name='Test')
        answers = ['Hi', 'A', 'a', 'Yes' if runs_ads else 'No'] + ([] if runs_ads else ['Referrals'])
        for message in answers:
            result = service.run(organization=organization, session_id='options', message=message)
            self.assertTrue(result.should_engage)
        saved = cache.get(service._session_cache_key(organization=organization, session_id='options'))
        state = saved['attributes']['_shvya_ai_qualification']
        self.assertEqual(state['qualification_status'], 'completed')
        self.assertEqual(state['requirement_states'][self.requirements[0]['id']]['value'], 'Slow replies')
        self.assertEqual(state['requirement_states'][self.requirements[1]['id']]['value'], 'WhatsApp')
        last_status = state['requirement_states'][self.requirements[-1]['id']]['status']
        self.assertEqual(last_status, 'not_applicable' if runs_ads else 'answered')
        self.assertEqual(provider.generate_text.call_count, len(answers))
        service.reset(organization=organization, session_id='options')
        self.assertIsNone(cache.get(service._session_cache_key(organization=organization, session_id='options')))

    def test_sandbox_accepts_letters_and_no_with_punctuated_options(self):
        self._sandbox(False)

    def test_sandbox_skips_inapplicable_ads_question(self):
        self._sandbox(True)


class DurableExecutionTests(TestCase):
    setUp = controls.AIEngagementControlTests.setUp
    _inbound = controls.AIEngagementControlTests._inbound

    def test_duplicate_worker_cannot_claim_same_inbound_turn_twice(self):
        from apps.ai_engagement.services.execution_tracker import claim_execution

        source = self._inbound()

        self.assertTrue(claim_execution(source.pk))
        self.assertFalse(claim_execution(source.pk))

    def test_stale_execution_claim_can_be_recovered(self):
        from apps.ai_engagement.services.execution_tracker import (
            KEY,
            claim_execution,
        )

        source = self._inbound()
        self.assertTrue(claim_execution(source.pk))
        source.refresh_from_db()
        payload = dict(source.raw_payload or {})
        execution = dict(payload.get(KEY) or {})
        execution["updated_at"] = (
            timezone.now() - timedelta(minutes=4)
        ).isoformat()
        payload[KEY] = execution
        source.raw_payload = payload
        source.save(update_fields=["raw_payload"])

        self.assertTrue(claim_execution(source.pk))

    def test_generation_failure_is_recorded_without_provider_error_contents(self):
        from apps.ai_engagement.tasks import _execute_ai_engagement_response
        source = self._inbound()
        with patch('apps.ai_engagement.tasks._execute_ai_engagement_response_impl', return_value={
                'status': 'failed', 'reason': 'engagement_generation_failed', 'error': 'SECRET provider details'}):
            _execute_ai_engagement_response(task=Mock(), lead_id=self.lead.pk)
        source.refresh_from_db()
        self.assertEqual(source.raw_payload['shvya_ai_execution']['status'], 'failed')
        self.assertNotIn('SECRET', json.dumps(source.raw_payload))

    def test_failed_broker_publication_survives_and_recovers(self):
        from apps.ai_engagement.services.execution_tracker import queue_api_engagement, recover_api_engagement
        source = self._inbound()
        with patch('apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async', side_effect=RuntimeError('broker unavailable')):
            queue_api_engagement(lead_id=self.lead.pk)
        source.refresh_from_db()
        self.assertEqual(source.raw_payload['shvya_ai_execution']['status'], 'queued')
        source.raw_payload['shvya_ai_execution']['updated_at'] = (timezone.now() - timedelta(minutes=2)).isoformat()
        source.save(update_fields=['raw_payload'])
        with patch('apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async') as enqueue:
            self.assertEqual(recover_api_engagement()['requeued'], 1)
            recover_api_engagement()
        enqueue.assert_called_once()

    def test_recovery_never_replays_untracked_historical_messages(self):
        from apps.ai_engagement.services.execution_tracker import recover_api_engagement
        self._inbound()
        with patch('apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async') as enqueue:
            self.assertEqual(recover_api_engagement()['requeued'], 0)
        enqueue.assert_not_called()

    def test_recovery_does_not_retry_permanent_failure(self):
        from apps.ai_engagement.services.execution_tracker import record_execution, recover_api_engagement
        source = self._inbound()
        record_execution(source.pk, status='failed', reason='engagement_generation_failed')
        with patch('apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async') as enqueue:
            recover_api_engagement()
        enqueue.assert_not_called()

    def test_committed_reply_with_lost_send_dispatch_is_recovered_once(self):
        from apps.channels.models import WhatsAppMessage
        from apps.ai_engagement.services.execution_tracker import record_execution, recover_api_engagement
        source = self._inbound()
        record_execution(source.pk, status='completed')
        outgoing = WhatsAppMessage.objects.create(organization=self.organization, account=self.account, lead=self.lead,
            direction='outbound', status='queued', body='Hello', raw_payload={'shvya_ai': {'source_inbound_message_id': str(source.pk)}})
        WhatsAppMessage.objects.filter(pk=outgoing.pk).update(created_at=timezone.now()-timedelta(minutes=2))
        with patch('apps.channels.tasks.send_whatsapp_message_task.delay') as send:
            recover_api_engagement()
            recover_api_engagement()
        send.assert_called_once_with(str(outgoing.pk))

class ReplyStatusIsolationTests(TestCase):
    setUp = controls.AIEngagementControlTests.setUp
    _inbound = controls.AIEngagementControlTests._inbound

    def test_status_requires_dashboard_login(self):
        response = self.client.get(f'/dashboard/whatsapp/leads/{self.lead.pk}/ai-status/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/dashboard/login/', response.url)

    def test_status_cannot_read_another_organization(self):
        from django.http import Http404
        from django.test import RequestFactory
        from apps.channels.ai_reply_status_ui import ai_reply_status
        from apps.organizations.models import Organization
        request = RequestFactory().get('/')
        request.crm_user = SimpleNamespace(organization=Organization.objects.create(name='Other'))
        with self.assertRaises(Http404):
            ai_reply_status.__wrapped__(request, lead_id=self.lead.pk)

    def test_status_renders_saved_failure_without_private_error(self):
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory
        from apps.channels.ai_reply_status_ui import ai_reply_status
        from apps.ai_engagement.services.execution_tracker import record_execution
        source = self._inbound()
        record_execution(source.pk, status='failed', reason='engagement_generation_failed')
        request = RequestFactory().get('/')
        request.crm_user = get_user_model().objects.create_user(email='status@example.com', password='test-only', organization=self.organization)
        request.user = request.crm_user
        response = ai_reply_status.__wrapped__(request, lead_id=self.lead.pk)
        self.assertContains(response, 'Reply could not be completed')
        self.assertNotContains(response, 'raw_payload')


class DurableBacklogRecoveryTests(TestCase):
    setUp = controls.AIEngagementControlTests.setUp
    _inbound = controls.AIEngagementControlTests._inbound

    def _outbound(self, *, payload=None, age_hours=2):
        from apps.channels.models import WhatsAppMessage

        message = WhatsAppMessage.objects.create(
            organization=self.organization, account=self.account, lead=self.lead,
            direction='outbound', status='queued', body='Pending AI message',
            raw_payload=payload or {'shvya_ai': {'origin': 'engagement'}},
        )
        WhatsAppMessage.objects.filter(pk=message.pk).update(
            created_at=timezone.now() - timedelta(hours=age_hours),
        )
        message.refresh_from_db()
        return message

    @patch('apps.channels.tasks.send_whatsapp_message_task.delay')
    def test_abandoned_inflight_head_is_failed_without_replay_and_queue_advances(self, send):
        from apps.ai_engagement.services.execution_tracker import recover_api_engagement
        from apps.channels.models import WhatsAppMessage

        abandoned = self._outbound()
        ready = self._outbound()
        fresh = self._outbound()
        WhatsAppMessage.objects.filter(pk=abandoned.pk).update(
            status='sending', updated_at=timezone.now() - timedelta(minutes=11),
        )
        WhatsAppMessage.objects.filter(pk=fresh.pk).update(status='sending')
        self.assertEqual(recover_api_engagement()['requeued'], 1)
        abandoned.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(abandoned.status, 'failed')
        self.assertEqual(abandoned.raw_payload['shvya_ai_delivery']['reason'], 'provider_outcome_uncertain')
        self.assertIsNone(abandoned.sent_at)
        self.assertEqual(fresh.status, 'sending')
        send.assert_called_once_with(str(ready.pk))
        from services.channels.ai_send_gate import _priority_wait
        self.assertIsNone(_priority_wait(ready, timezone.now()))

    @patch('apps.channels.tasks.send_whatsapp_message_task.delay')
    def test_backlog_older_than_one_hour_and_without_source_is_recovered(self, send):
        from apps.ai_engagement.services.execution_tracker import recover_api_engagement

        welcome = self._outbound(payload={'shvya_welcome': {'origin': 'welcome'}})
        bump = self._outbound(payload={'shvya_ai': {'origin': 'bump_up'}})
        self.assertEqual(recover_api_engagement()['requeued'], 2)
        self.assertEqual([call.args[0] for call in send.call_args_list], [str(welcome.pk), str(bump.pk)])
        self.assertEqual(recover_api_engagement()['requeued'], 0)

    @patch('apps.channels.tasks.send_whatsapp_message_task.delay')
    def test_more_than_one_batch_drains_without_first_hundred_starvation(self, send):
        from apps.ai_engagement.services.execution_tracker import recover_api_engagement

        messages = [self._outbound() for _ in range(125)]
        self.assertEqual(recover_api_engagement()['requeued'], 100)
        self.assertEqual(recover_api_engagement()['requeued'], 25)
        self.assertEqual(recover_api_engagement()['requeued'], 0)
        self.assertEqual({call.args[0] for call in send.call_args_list}, {str(m.pk) for m in messages})

    @patch('apps.channels.tasks.send_whatsapp_message_task.delay')
    def test_future_gap_wait_does_not_hide_eligible_backlog(self, send):
        from apps.ai_engagement.services.execution_tracker import recover_api_engagement

        for _ in range(100):
            self._outbound(payload={
                'shvya_ai': {'origin': 'engagement'},
                'shvya_ai_delivery': {'available_at': (timezone.now() + timedelta(minutes=5)).isoformat()},
            })
        due = self._outbound()
        self.assertEqual(recover_api_engagement()['requeued'], 1)
        send.assert_called_once_with(str(due.pk))

    @patch('apps.channels.tasks.send_whatsapp_message_task.delay')
    def test_job_owned_welcomes_and_replies_are_left_to_job_recovery(self, send):
        from apps.ai_engagement.services.execution_tracker import recover_api_engagement

        self._outbound(payload={'shvya_welcome': {'job_id': 'welcome-job'}})
        self._outbound(payload={'shvya_ai': {'job_id': 'reply-job'}})
        self.assertEqual(recover_api_engagement()['requeued'], 0)
        send.assert_not_called()

    @patch('apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async')
    def test_old_tracked_inbound_is_not_abandoned_or_hidden_by_fresh_claims(self, publish):
        from apps.channels.models import WhatsAppMessage
        from apps.ai_engagement.services.execution_tracker import KEY, recover_api_engagement

        now = timezone.now()
        for index in range(100):
            message = self._inbound(external_id=f'fresh-{index}')
            WhatsAppMessage.objects.filter(pk=message.pk).update(raw_payload={
                KEY: {'status': 'processing', 'updated_at': now.isoformat(), 'attempts': 1},
            })
        due = self._inbound(external_id='due-last')
        # Keep this the latest inbound while making every row an old backlog.
        WhatsAppMessage.objects.filter(lead=self.lead).update(created_at=now - timedelta(hours=3))
        WhatsAppMessage.objects.filter(pk=due.pk).update(
            created_at=now - timedelta(hours=2),
            raw_payload={KEY: {'status': 'queued', 'updated_at': (now - timedelta(minutes=2)).isoformat()}},
        )
        self.assertEqual(recover_api_engagement()['requeued'], 1)
        publish.assert_called_once_with(args=[str(self.lead.pk)], countdown=0)


class ArrivalOrderFinalizerTests(TestCase):
    setUp = controls.AIEngagementControlTests.setUp
    _inbound = controls.AIEngagementControlTests._inbound

    def test_reversed_generation_completion_preserves_customer_arrival_order(self):
        from apps.ai_engagement.services.engagement_execution import _execute_ai_engagement_response_impl
        from apps.channels.models import WhatsAppMessage
        from apps.crm.models import Lead

        first = self._inbound(external_id='arrival-first')
        second_lead = Lead.objects.create(
            organization=self.organization, pipeline=self.pipeline, stage=self.new_lead,
            name='Second arrival', phone='+919111111112', ai_enabled=True,
        )
        second = WhatsAppMessage.objects.create(
            organization=self.organization, account=self.account, lead=second_lead,
            direction='inbound', body='Later arrival', status='received',
        )
        decision = SimpleNamespace(
            should_engage=True, message='Your answer', model='test', reason='NORMAL_CONVERSATION',
            file_document_id=None, crm_actions=[], next_requirement_id=None,
        )
        with (
            patch('apps.ai_engagement.services.ai_permissions.AIPermissionService.evaluate',
                  return_value=SimpleNamespace(allowed=True, reason='allowed')),
            patch('apps.ai_engagement.services.engagement.EngagementService.engage', return_value=decision),
            patch('services.channels.whatsapp_service.resolve_account_for_lead', return_value=self.account),
            patch('apps.ai_engagement.services.engagement_execution._persist_engagement_answers', return_value=True),
            patch('apps.ai_engagement.services.crm_executor.CRMActionExecutor.execute', return_value={}),
            patch('apps.channels.tasks.send_whatsapp_message_task.delay'),
        ):
            later_result = _execute_ai_engagement_response_impl(task=Mock(), lead_id=second_lead.pk)
            earlier_result = _execute_ai_engagement_response_impl(task=Mock(), lead_id=self.lead.pk)

        self.assertEqual(later_result['status'], 'completed', later_result)
        self.assertEqual(earlier_result['status'], 'completed', earlier_result)
        later_reply = WhatsAppMessage.objects.get(pk=later_result['message_id'])
        earlier_reply = WhatsAppMessage.objects.get(pk=earlier_result['message_id'])
        self.assertLess(later_reply.created_at, earlier_reply.created_at)
        self.assertEqual(earlier_reply.raw_payload['shvya_ai']['queued_at'], first.created_at.isoformat())
        self.assertEqual(later_reply.raw_payload['shvya_ai']['queued_at'], second.created_at.isoformat())
        self.assertLess(
            earlier_reply.raw_payload['shvya_ai']['queued_at'],
            later_reply.raw_payload['shvya_ai']['queued_at'],
        )
