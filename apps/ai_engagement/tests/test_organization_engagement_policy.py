import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase

from apps.ai_engagement.graph.workflow import _route_turn
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.engagement import EngagementService, EngagementDecision, EngagementError
from apps.ai_engagement.services.playground import PlaygroundService, PlaygroundError
from services.channels import whatsapp_service


class OrganizationEngagementPolicyTests(SimpleTestCase):
    def test_inbound_keywords_do_not_disable_lead(self):
        for text in ['STOP', 'unsubscribe', 'no', 'not interested', 'Please do not contact me']:
            lead = SimpleNamespace(ai_enabled=True, notes='', save=Mock())
            whatsapp_service._apply_reply_intent(lead=lead, body=text)
            self.assertTrue(lead.ai_enabled, text)

    def test_graph_does_not_silently_route_keywords(self):
        for text in ['STOP', 'unsubscribe', 'no', 'thanks', 'hello']:
            # dataclasses.replace requires the actual context type.
            context = AIContext(organization={}, lead={}, pipeline={}, stage={},
                                contacts=[], attributes=[], conversation={},
                                conversation_summary=None, qualification_notes=[], knowledge=[])
            result = _route_turn({'latest_text': text, 'caller_supplied_context': True,
                                  'context': context, 'service': EngagementService()})
            self.assertEqual(result['route'], 'generate')
            self.assertNotIn('direct_decision', result)

    def test_silence_requires_organization_rule_evidence(self):
        service = EngagementService()
        for field in ['qualification_requirements', 'engagement_instructions']:
            rule = 'Do not reply when the lead says pause.'
            context = SimpleNamespace(organization={field: rule})
            decision = EngagementDecision(False, '', None, [], 'ORG_INSTRUCTION', 'test',
                reason_code='ORG_INSTRUCTION', silence_rule={'field': field, 'quote': rule})
            service._validate_engagement_policy(decision=decision, context=context)
            for bad_context in [SimpleNamespace(organization={}),
                                SimpleNamespace(organization={field: 'Be friendly.'})]:
                with self.assertRaises(EngagementError):
                    service._validate_engagement_policy(decision=decision, context=bad_context)

    def test_legacy_no_action_cannot_authorize_silence(self):
        decision = EngagementDecision(False, '', None, [], 'NO_ACTION', 'test')
        with self.assertRaises(EngagementError):
            EngagementService()._validate_engagement_policy(
                decision=decision, context=SimpleNamespace(organization={'engagement_instructions':'Be friendly.'}))


class PlaygroundEngagementPolicyTests(SimpleTestCase):
    def run_turn(self, results, **instructions):
        provider = Mock()
        provider.generate_text.side_effect = [AITextResult(json.dumps(item), 'test') for item in results]
        info = Mock()
        info.get_or_create.return_value = SimpleNamespace(ai_enabled=True, about='Org',
            bot_languages='English', qualification_requirements='', engagement_instructions='',
            bump_up_enabled=False, bump_up_count=0)
        for key, value in instructions.items():
            setattr(info.get_or_create.return_value, key, value)
        retrieval = Mock()
        retrieval.retrieve_by_vector.return_value = []
        service = PlaygroundService(provider=provider, org_info_service=info,
            embedding_service=Mock(), retrieval_service=retrieval)
        return service.run(organization=SimpleNamespace(id='org', name='Org'),
                           session_id='test', message='no', history=[]), provider

    def payload(self, engage=False, rule=None):
        return {'should_engage':engage, 'message':'How can I help?' if engage else '',
                'silence_rule':rule, 'file_document_id':None, 'crm_actions':[],
                'qualification_updates':[], 'next_requirement_id':None,
                'reason_code':'NORMAL_CONVERSATION' if engage else ('ORG_INSTRUCTION' if rule else 'NO_ACTION')}

    def test_unjustified_silence_is_repaired_into_reply(self):
        result, provider = self.run_turn([self.payload(), self.payload(True)])
        self.assertTrue(result.should_engage)
        self.assertEqual(provider.generate_text.call_count, 2)
        metadata = provider.generate_text.call_args.kwargs['metadata']
        self.assertEqual(metadata['task'], 'playground')
        self.assertEqual(metadata['session_id'], 'test')
        self.assertNotIn('lead_id', metadata)

    def test_repeated_invalid_silence_is_error_not_configured_skip(self):
        with self.assertRaises(PlaygroundError):
            self.run_turn([self.payload(), self.payload()])

    def test_authored_silence_supported_in_both_ai_setup_fields(self):
        for field in ['qualification_requirements', 'engagement_instructions']:
            rule = 'Do not reply to no.'
            result, provider = self.run_turn([self.payload(rule={'field':field,'quote':rule})], **{field:rule})
            self.assertFalse(result.should_engage)
            provider.generate_text.assert_called_once()


def test_production_celery_startup_registers_both_inbound_paths():
    env = dict(os.environ, SECRET_KEY='startup-test-only', DJANGO_SETTINGS_MODULE='config.settings.prod')
    result = subprocess.run([sys.executable, '-c', '''
from config.celery import app
app.loader.init_worker()
for task, queue in [
    ('ai.generate_ai_engagement_response', 'ai_realtime'),
    ('apps.channels.tasks.send_whatsapp_message_task', 'ai_realtime'),
    ('apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task', 'hosted_ai'),
    ('hosted.dispatch_due_ai', 'hosted_ai'),
]:
    assert task in app.tasks, task
    assert app.conf.task_routes[task]['queue'] == queue
from apps.ai_engagement.services.engagement import EngagementService
assert hasattr(EngagementService, '_langgraph_legacy_engage')
from apps.ai_engagement.graph.workflow import run_engagement_graph
assert callable(run_engagement_graph)
print('Production Celery startup and inbound task registration passed')
'''], cwd=Path(__file__).resolve().parents[3], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_readiness_requires_tasks_on_the_consuming_worker():
    from apps.ai_engagement.management.commands.check_ai_runtime import REQUIRED, missing_consumers
    queues = {'api': [{'name': 'ai_realtime'}], 'hosted': [{'name': 'hosted_ai'}]}
    registered = {'api': list(REQUIRED['ai_realtime']), 'hosted': list(REQUIRED['hosted_ai'])}
    assert missing_consumers(queues, registered) == []
    assert missing_consumers(queues, {}) == ['ai_realtime', 'hosted_ai']
    assert missing_consumers({}, registered) == ['ai_realtime', 'hosted_ai']
    registered['hosted'] = []
    assert missing_consumers(queues, registered) == ['hosted_ai']
