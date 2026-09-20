from tests.playbook_fixtures import build_ai_playbook

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
            embedding_service=Mock(), retrieval_service=retrieval)
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

    def test_manual_queue_pins_exact_source_and_account(self):
        from apps.ai_engagement.services.execution_tracker import (
            active_pinned_source,
            queue_api_engagement,
        )
        from apps.ai_engagement.tasks import _latest_whatsapp_message
        from apps.channels.models import WhatsAppAccount, WhatsAppMessage
        from services.channels.whatsapp_service import resolve_account_for_lead

        source = self._inbound()
        other = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Other number",
            phone_number_id="other-phone-id",
            display_phone_number="+919999999998",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=other,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wamid-other-newer",
            from_number=self.lead.phone,
            to_number=other.display_phone_number,
            body="Newer message on another account",
            status=WhatsAppMessage.Status.RECEIVED,
        )

        with patch(
            "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
        ) as enqueue:
            queue_api_engagement(
                lead_id=self.lead.pk,
                source_message_id=source.pk,
                account_id=self.account.pk,
            )

        source.refresh_from_db()
        execution = source.raw_payload["shvya_ai_execution"]
        self.assertTrue(execution["pinned_source"])
        self.assertEqual(execution["account_id"], str(self.account.pk))
        self.assertEqual(execution["status"], "queued")
        self.assertEqual(active_pinned_source(lead=self.lead).pk, source.pk)
        self.assertEqual(_latest_whatsapp_message(lead=self.lead).pk, source.pk)
        self.assertEqual(
            resolve_account_for_lead(
                organization=self.organization,
                lead=self.lead,
            ).pk,
            self.account.pk,
        )
        enqueue.assert_called_once_with(args=[str(self.lead.pk)], countdown=0)

    def test_worker_claims_pinned_source_not_newer_other_account(self):
        from apps.ai_engagement.services.execution_tracker import queue_api_engagement
        from apps.ai_engagement.tasks import _execute_ai_engagement_response
        from apps.channels.models import WhatsAppAccount, WhatsAppMessage

        source = self._inbound(external_id="wamid-pinned-worker")
        other = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Other worker number",
            phone_number_id="other-worker-phone-id",
            display_phone_number="+919999999997",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        newer = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=other,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wamid-other-worker-newer",
            from_number=self.lead.phone,
            to_number=other.display_phone_number,
            body="Newer message on another account",
            status=WhatsAppMessage.Status.RECEIVED,
        )
        with patch(
            "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
        ):
            queue_api_engagement(
                lead_id=self.lead.pk,
                source_message_id=source.pk,
                account_id=self.account.pk,
            )

        with patch(
            "apps.ai_engagement.tasks._execute_ai_engagement_response_impl",
            return_value={"status": "completed", "reason": ""},
        ):
            result = _execute_ai_engagement_response(
                task=Mock(),
                lead_id=self.lead.pk,
            )

        self.assertEqual(result["status"], "completed")
        source.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(
            source.raw_payload["shvya_ai_execution"]["status"],
            "completed",
        )
        self.assertNotIn("shvya_ai_execution", newer.raw_payload)

    def test_manual_queue_rejects_historical_source(self):
        from apps.ai_engagement.services.execution_tracker import queue_api_engagement

        source = self._inbound(external_id="wamid-historical-manual")
        source.media_payload = {"coexistence_sync": True, "historical": True}
        source.save(update_fields=["media_payload", "updated_at"])

        with patch(
            "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
        ) as enqueue:
            result = queue_api_engagement(
                lead_id=self.lead.pk,
                source_message_id=source.pk,
                account_id=self.account.pk,
            )

        self.assertIsNone(result)
        enqueue.assert_not_called()
        source.refresh_from_db()
        self.assertEqual(
            source.raw_payload["shvya_ai_execution"]["reason"],
            "historical_source",
        )

    def test_manual_queue_is_superseded_by_newer_same_account_message(self):
        from apps.ai_engagement.services.execution_tracker import queue_api_engagement

        source = self._inbound(external_id="wamid-manual-source")
        self._inbound(external_id="wamid-newer-same-account")

        with patch(
            "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
        ) as enqueue:
            result = queue_api_engagement(
                lead_id=self.lead.pk,
                source_message_id=source.pk,
                account_id=self.account.pk,
            )

        self.assertIsNone(result)
        enqueue.assert_not_called()
        source.refresh_from_db()
        self.assertEqual(
            source.raw_payload["shvya_ai_execution"]["reason"],
            "conversation_changed",
        )

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
