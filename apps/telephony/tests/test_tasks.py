import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.ai_engagement.services.ai_provider import (
    AIProviderConfigurationError, AIProviderTransientError, OpenAIProvider,
)
from apps.telephony.models import CallIntelligenceResult, CallRecord
from apps.telephony.services import call_analysis_hash, request_call_analysis
from apps.telephony.tasks import analyze_call_intelligence, recover_call_intelligence

from . import test_mobile_workspace as fixtures


def analysis_response(summary="Customer requested a demo and follow-up on Tuesday."):
    return SimpleNamespace(text=json.dumps({
        "summary": summary, "intent": "high", "sentiment": "positive",
        "outcome": "Demo requested", "objections": [], "buying_signals": ["Requested a demo"],
        "competitor": "", "budget": "", "timeline": "Tuesday", "product_interest": "",
        "decision_maker": "unknown", "next_action": "Arrange a demo",
        "follow_up_at": "", "ai_score": 8, "qualification_score": 6,
        "agent_metrics": {}, "compliance_flags": [], "attributes": [],
    }), model="configured-call-model")


class CallAnalysisTests(TestCase):
    setUp = fixtures.MobileWorkspaceTests.setUp
    payload = fixtures.MobileWorkspaceTests.payload
    call = fixtures.MobileWorkspaceTests.call
    api = fixtures.MobileWorkspaceTests.api
    web = fixtures.MobileWorkspaceTests.web

    @patch("apps.telephony.tasks.OpenAIProvider")
    def test_notes_produce_grounded_intelligence_once_and_keep_crm_stage(self, provider):
        call = self.call(notes="Customer requested a demo on Tuesday.")
        original_stage = call.lead.stage_id
        provider.return_value.generate_text.return_value = analysis_response()
        analyze_call_intelligence.run(str(call.id), call_analysis_hash(call))
        analyze_call_intelligence.run(str(call.id), call_analysis_hash(call))
        call.refresh_from_db()
        self.assertEqual(call.analysis_status, "completed")
        self.assertEqual(call.intelligence.summary, "Customer requested a demo and follow-up on Tuesday.")
        self.assertEqual(call.intelligence.intent, "high")
        self.assertEqual(call.lead.stage_id, original_stage)
        self.assertEqual(call.crm_call.notes, call.notes)
        provider.return_value.generate_text.assert_called_once()
        supplied = json.loads(provider.return_value.generate_text.call_args.kwargs["input_text"])
        self.assertEqual(supplied["call"]["notes"], call.notes)
        self.assertEqual(supplied["lead"]["name"], call.lead.name)

    @patch("apps.telephony.tasks.analyze_call_intelligence.delay")
    @patch("apps.telephony.tasks.OpenAIProvider")
    def test_web_save_queues_after_commit_and_status_returns_live_escaped_summary(self, provider, publish):
        call = self.call()
        provider.return_value.generate_text.return_value = analysis_response("Demo requested <script>alert(1)</script>")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.web().post(
                reverse("call-intelligence-call-action", args=[call.id]),
                {"notes": "Asked for a demo", "disposition": "interested"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            publish.assert_not_called()
        self.assertEqual(response.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.analysis_status, "queued")
        self.assertEqual(call.crm_call.notes, "Asked for a demo")
        publish.assert_called_once_with(str(call.id), call.analysis_input_hash)
        analyze_call_intelligence.run(*publish.call_args.args)
        status = self.web().get(response.json()["status_url"])
        self.assertEqual(status.json()["analysis_status"], "completed")
        self.assertIn("&lt;script&gt;", status.json()["intelligence_html"])
        self.assertNotIn("<script>", status.json()["intelligence_html"])
        self.assertEqual(status["Cache-Control"], "no-store")
        self.assertEqual(self.web(self.other_user).get(response.json()["status_url"]).status_code, 404)

    @patch("apps.telephony.tasks.OpenAIProvider")
    def test_provider_configuration_failure_is_visible_and_retryable_from_save(self, provider):
        call = self.call(notes="Asked for a demo.")
        provider.side_effect = AIProviderConfigurationError("Never expose this private configuration detail")
        analyze_call_intelligence.run(str(call.id), call_analysis_hash(call))
        call.refresh_from_db()
        self.assertEqual(call.analysis_status, "failed")
        self.assertIn("administrator", call.analysis_error)
        self.assertNotIn("private configuration", call.analysis_error)
        self.assertFalse(CallIntelligenceResult.objects.filter(call=call).exists())
        response = self.web().get(reverse("call-intelligence-call-status", args=[call.id]))
        self.assertEqual(response.json()["analysis_status"], "failed")
        self.assertIn("administrator", response.json()["intelligence_html"])
        queued = request_call_analysis(call.id, retry_failed=True)
        self.assertEqual(queued.analysis_status, "queued")
        self.assertEqual(queued.analysis_attempts, 0)

    @patch("apps.telephony.tasks.OpenAIProvider")
    @override_settings(CELERY_TASK_EAGER_PROPAGATES=False)
    def test_transient_failures_have_three_attempts_and_then_visible_failure(self, provider):
        call = self.call(notes="Requested a demo.")
        provider.return_value.generate_text.side_effect = AIProviderTransientError("Temporary failure")
        result = analyze_call_intelligence.apply(args=[str(call.id), call_analysis_hash(call)])
        self.assertTrue(result.successful())
        call.refresh_from_db()
        self.assertEqual(call.analysis_attempts, 3)
        self.assertEqual(call.analysis_status, "failed")
        self.assertEqual(provider.return_value.generate_text.call_count, 3)

    @patch("apps.telephony.tasks.OpenAIProvider")
    @override_settings(CELERY_TASK_EAGER_PROPAGATES=False)
    def test_truncated_json_is_retried_and_reported_instead_of_staying_queued(self, provider):
        call = self.call(notes="Requested a demo.")
        provider.return_value.generate_text.return_value = SimpleNamespace(text='{"summary":', model="model")
        analyze_call_intelligence.apply(args=[str(call.id), call_analysis_hash(call)])
        call.refresh_from_db()
        self.assertEqual(call.analysis_status, "failed")
        self.assertEqual(call.analysis_attempts, 3)
        self.assertIn("incomplete result", call.analysis_error)

    @patch("apps.telephony.tasks.OpenAIProvider")
    def test_old_provider_result_cannot_overwrite_newer_notes(self, provider):
        call = self.call(notes="Old evidence.")
        old_hash = call_analysis_hash(call)

        def update_notes_while_analyzing(**kwargs):
            CallRecord.objects.filter(pk=call.id).update(notes="New evidence: no longer interested.")
            request_call_analysis(call.id)
            return analysis_response("Old summary must not be saved")

        provider.return_value.generate_text.side_effect = update_notes_while_analyzing
        analyze_call_intelligence.run(str(call.id), old_hash)
        call.refresh_from_db()
        self.assertEqual(call.analysis_status, "queued")
        self.assertNotEqual(call.analysis_input_hash, old_hash)
        self.assertFalse(CallIntelligenceResult.objects.filter(call=call).exists())
        provider.reset_mock()
        analyze_call_intelligence.run(str(call.id), old_hash)
        provider.assert_not_called()

    @patch("apps.telephony.tasks.analyze_call_intelligence.delay", side_effect=ConnectionError("Broker unavailable"))
    def test_broker_interruption_keeps_saved_notes_and_recovery_republishes(self, publish):
        call = self.call()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api().patch(
                f"/api/v1/call-intelligence/calls/{call.id}/notes/",
                {"notes": "Please arrange a demo."}, format="json",
            )
        self.assertEqual(response.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.notes, "Please arrange a demo.")
        self.assertEqual(call.analysis_status, "queued")
        self.assertIn("reconnect", call.analysis_error)
        CallRecord.objects.filter(pk=call.id).update(
            analysis_updated_at=timezone.now() - timedelta(minutes=6),
        )
        publish.side_effect = None
        publish.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            recover_call_intelligence.run()
        publish.assert_called_once_with(str(call.id), call.analysis_input_hash)

    @patch("apps.telephony.tasks.OpenAIProvider")
    def test_removing_notes_clears_old_intelligence_and_does_not_analyze_empty_evidence(self, provider):
        call = self.call(notes="Requested a demo.")
        provider.return_value.generate_text.return_value = analysis_response()
        analyze_call_intelligence.run(str(call.id), call_analysis_hash(call))
        response = self.api().patch(
            f"/api/v1/call-intelligence/calls/{call.id}/notes/", {"notes": ""}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["call"]["analysis_status"], "not_requested")
        self.assertIsNone(response.data["call"]["intelligence"])
        call.refresh_from_db()
        self.assertEqual(call.crm_call.notes, "")
        analyze_call_intelligence.run(str(call.id))
        self.assertEqual(provider.return_value.generate_text.call_count, 1)

    def test_invalid_unlinked_followup_does_not_partially_save_notes(self):
        from apps.telephony.models import CallIntelligenceSettings

        CallIntelligenceSettings.objects.filter(organization=self.org).update(enabled=False)
        call = self.call()
        response = self.web().post(
            reverse("call-intelligence-call-action", args=[call.id]),
            {"notes": "Should not be saved", "follow_up_at": (timezone.now()+timedelta(days=1)).isoformat()},
        )
        self.assertEqual(response.status_code, 400)
        call.refresh_from_db()
        self.assertEqual(call.notes, "")


class CallAnalysisTokenBudgetTests(SimpleTestCase):
    @override_settings(OPENAI_API_KEY="local-test-only")
    @patch.dict("os.environ", {"OPENAI_CALL_INTELLIGENCE_MAX_OUTPUT_TOKENS": "2000"})
    def test_call_analysis_budget_covers_full_structured_result(self):
        provider = OpenAIProvider(client=Mock())
        self.assertEqual(provider._max_output_tokens({"task": "call_intelligence"}), 2000)
        self.assertEqual(OpenAIProvider.TASK_MAX_OUTPUT_TOKENS["call_intelligence"], 2000)
