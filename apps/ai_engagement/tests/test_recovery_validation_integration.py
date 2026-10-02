"""Readiness, no-send replay, tenant boundaries and typed failure regressions."""
import io
import json
import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from apps.ai_engagement.models import Chunk, Document, FAQ, OrgInfo
from apps.ai_engagement.services import evidence_recovery as recovery
from apps.ai_engagement.services import recovery_evaluation as evaluation
from apps.ai_engagement.services.ai_provider import (
    AIProviderConfigurationError, AIProviderPermanentError, AIProviderTransientError,
)
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.playground import _SandboxLead
from apps.ai_engagement.services.recovery_scenarios import RecoveryEvaluationError
from apps.crm.models import Lead, LeadReminder, Pipeline
from apps.organizations.models import Organization


@dataclass
class Context:
    organization: dict
    lead: dict = field(default_factory=dict)
    conversation: dict = field(default_factory=lambda: {"messages": []})
    knowledge: list = field(default_factory=list)


def scenario(channel="instagram"):
    return {"version": 1, "cases": [{"id": "example", "channel": channel,
        "turns": [{"message": "Tell me the package details", "expect": {"contains_all": ["details"]}}]}]}


class TypedCoverageFailureTests(SimpleTestCase):
    def state(self):
        return {"started_at": monotonic(), "context": Context({}),
                "organization": SimpleNamespace(id="org"), "lead": SimpleNamespace(id="lead")}

    def test_provider_failures_are_not_missing_evidence(self):
        for error, code in ((AIProviderConfigurationError("PRIVATE_KEY"), "provider_configuration_error"),
                            (AIProviderPermanentError("PRIVATE_REASON"), "provider_rejected"),
                            (AIProviderTransientError("PRIVATE_TOKEN"), "provider_temporary_error")):
            with self.subTest(code=code), patch("apps.ai_engagement.services.ai_provider.OpenAIProvider", side_effect=error):
                result = recovery._assess(self.state(), [])
            self.assertEqual(result.status, code)
            self.assertNotIn("PRIVATE", str(result.summary()))

    def test_timeout_is_not_malformed_verdict(self):
        error = AIProviderTransientError("private timeout details")
        error.__cause__ = TimeoutError()
        with patch("apps.ai_engagement.services.ai_provider.OpenAIProvider", side_effect=error):
            self.assertEqual(recovery._assess(self.state(), []).status, "timeout")


@override_settings(OPENAI_API_KEY="test-only-not-a-real-key")
class RecoveryValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Internal replay fixture")
        cls.other = Organization.objects.create(name="Other replay tenant")
        cls.info, _ = OrgInfo.objects.get_or_create(organization=cls.organization)
        cls.info.about = "Approved example package details."
        cls.info.ai_enabled = True
        cls.info.save(update_fields=["about", "ai_enabled"])
        cls.pipeline = Pipeline.objects.create(organization=cls.organization, name="Replay Sales",
            country_code="+91", phone_number="9000000861")
        cls.lead = Lead.objects.create(organization=cls.organization, pipeline=cls.pipeline,
            stage=cls.pipeline.stages.get(name="New leads"), phone="+919000000862", name="Test visitor")

    def test_readiness_does_not_call_model_or_create_records(self):
        before = (Lead.objects.count(), OrgInfo.objects.count(), LeadReminder.objects.count())
        with patch("apps.ai_engagement.services.ai_provider.OpenAIProvider", side_effect=AssertionError("No model")):
            report = evaluation.preflight(self.organization)
        self.assertEqual(before, (Lead.objects.count(), OrgInfo.objects.count(), LeadReminder.objects.count()))
        self.assertFalse(report["live_model_evaluated"])
        self.assertFalse(report["activation_changed"])
        self.assertNotIn("test-only-not-a-real-key", json.dumps(report))

    def test_readiness_excludes_other_tenant_sources(self):
        doc = Document.objects.create(organization=self.other, name="Other", processing_status="completed")
        Chunk.objects.create(organization=self.other, document=doc, content="PRIVATE_OTHER_CONTENT")
        FAQ.objects.create(organization=self.other, question="Private?", answer="Other answer")
        report = evaluation.preflight(self.organization)
        self.assertEqual(report["source_counts"]["active_documents"], 0)
        self.assertEqual(report["source_counts"]["active_faqs"], 0)
        self.assertNotIn("PRIVATE_OTHER", json.dumps(report))

    def test_mixed_source_health_is_visible(self):
        Document.objects.create(organization=self.organization, name="Missing index", processing_status="completed")
        Document.objects.create(organization=self.organization, name="Failed", processing_status="failed")
        Document.objects.create(organization=self.organization, name="Pending", processing_status="pending")
        counts = evaluation.preflight(self.organization)["source_counts"]
        self.assertEqual(counts["completed_documents_without_chunks"], 1)
        self.assertEqual(counts["failed_documents"], 1)
        self.assertEqual(counts["unready_documents"], 1)

    def test_fingerprint_detects_content_update_without_timestamp(self):
        faq = FAQ.objects.create(organization=self.organization, question="Price?", answer="Original")
        before = evaluation.snapshot_fingerprint(self.organization)
        FAQ.objects.filter(pk=faq.pk).update(answer="Changed without timestamp")
        self.assertNotEqual(before, evaluation.snapshot_fingerprint(self.organization))

    def test_fingerprint_does_not_include_another_tenant(self):
        before = evaluation.snapshot_fingerprint(self.organization)
        FAQ.objects.create(organization=self.other, question="Other?", answer="Unrelated")
        self.assertEqual(before, evaluation.snapshot_fingerprint(self.organization))

    def test_fingerprint_row_budget_fails_closed(self):
        with patch.object(evaluation, "MAX_SNAPSHOT_ROWS", 0), self.assertRaises(RecoveryEvaluationError):
            evaluation.snapshot_fingerprint(self.organization)

    def test_real_lead_cannot_use_preview_override(self):
        with patch.dict(os.environ, {"AI_BRAIN_RECOVERY_ENABLED": "0"}):
            with recovery.sandbox_recovery_preview(organization_id=self.organization.pk, use_recovery=True):
                self.assertFalse(recovery.enabled({"organization": self.organization, "lead": self.lead}))
                visitor = _SandboxLead(organization_id=self.organization.pk)
                self.assertTrue(recovery.enabled({"organization": self.organization, "lead": visitor}))

    def test_memory_sessions_are_isolated_without_cache(self):
        runner, other_runner = evaluation._memory_playground(), evaluation._memory_playground()
        with patch("apps.ai_engagement.services.playground.cache.set", side_effect=AssertionError("No session cache writes")):
            runner._save_history(organization=self.organization, session_id="one", history=[], attributes={"tool": "Excel"})
        self.assertEqual(runner.attributes_for(organization=self.organization, session_id="one"), {"tool": "Excel"})
        self.assertEqual(runner.attributes_for(organization=self.other, session_id="one"), {})
        self.assertEqual(other_runner.attributes_for(organization=self.organization, session_id="one"), {})

    def fake_runner(self, records, callback=None):
        class Runner:
            def run(inner, **kwargs):
                visitor = _SandboxLead(organization_id=kwargs["organization"].pk)
                records.append(recovery.enabled({"organization": kwargs["organization"], "lead": visitor}))
                if callback:
                    callback()
                return SimpleNamespace(response="Example package details", model="recorded-test", events=[], files=[], stage={})

            def attributes_for(inner, **kwargs):
                return {}
        return Runner()

    def test_paired_replay_has_separate_off_on_scopes(self):
        records = []
        original = dict(os.environ)
        with patch.object(evaluation, "_memory_playground", side_effect=lambda: self.fake_runner(records)):
            result = evaluation.evaluate(self.organization, scenario())
        self.assertEqual(records, [False, True])
        self.assertEqual(original, dict(os.environ))
        self.assertEqual(result["completed_turns"], 2)
        self.assertTrue(result["comparison_valid"])
        self.assertFalse(result["activation_changed"])
        self.assertFalse(result["delivery_verified"])
        self.assertNotIn("Example package details", json.dumps(result))

    def test_replacing_fallback_does_not_invalidate_stable_comparison(self):
        class Runner:
            def run(inner, **kwargs):
                visitor = _SandboxLead(organization_id=kwargs["organization"].pk)
                active = recovery.enabled({"organization": kwargs["organization"], "lead": visitor})
                return SimpleNamespace(
                    response="Approved details" if active else "Please retry shortly",
                    model="recorded-test" if active else "fallback", events=[], files=[], stage={},
                )

            def attributes_for(inner, **kwargs):
                return {}

        with patch.object(evaluation, "_memory_playground", side_effect=Runner):
            report = evaluation.evaluate(self.organization, scenario())
        self.assertTrue(report["comparison_valid"])
        self.assertFalse(report["cases"][0]["model_labels_match"])
        self.assertFalse(report["acceptance"]["baseline"]["passed"])
        self.assertTrue(report["acceptance"]["recovery"]["passed"])
        self.assertTrue(report["requires_human_review"])
        self.assertFalse(report["semantic_accuracy_verified"])

    def test_turn_budget_checked_before_any_run(self):
        with patch.object(evaluation, "_memory_playground") as runner, self.assertRaises(RecoveryEvaluationError):
            evaluation.evaluate(self.organization, scenario(), max_turns=1)
        runner.assert_not_called()

    def test_foreign_stage_rejected_before_provider(self):
        other_pipeline = Pipeline.objects.create(organization=self.other, name="Other sales",
            country_code="+91", phone_number="9000000863")
        data = scenario()
        data["cases"][0]["stage_id"] = str(other_pipeline.stages.first().pk)
        with patch.object(evaluation, "_memory_playground") as runner, self.assertRaises(RecoveryEvaluationError):
            evaluation.evaluate(self.organization, data)
        runner.assert_not_called()

    def test_drift_stops_remaining_variants(self):
        records = []
        faq = FAQ.objects.create(organization=self.organization, question="Price?", answer="Original")
        def change_source():
            FAQ.objects.filter(pk=faq.pk).update(answer="Changed")
        with patch.object(evaluation, "_memory_playground", side_effect=lambda: self.fake_runner(records, change_source)):
            report = evaluation.evaluate(self.organization, scenario())
        self.assertEqual(records, [False])
        self.assertFalse(report["source_snapshot_unchanged"])
        self.assertEqual(report["stopped_reason"], "source_changed_during_comparison")
        self.assertIsNone(recovery._SANDBOX_RECOVERY.get())

    def test_runtime_error_is_redacted(self):
        def fail():
            raise RuntimeError("PRIVATE_TOKEN_AND_REPLY")
        with patch.object(evaluation, "_memory_playground", side_effect=lambda: self.fake_runner([], fail)):
            report = evaluation.evaluate(self.organization, scenario())
        self.assertFalse(report["comparison_valid"])
        self.assertEqual(report["error_type"], "RuntimeError")
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_database_error_does_not_poison_the_callers_transaction(self):
        def invalid_read(state):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
        state = {"organization": self.organization, "lead": self.lead,
            "service": SimpleNamespace(_validate_context_scope=Mock()),
            "context": Context({"id": str(self.organization.pk)}), "latest_text": "Price?"}
        with patch.dict(os.environ, {"AI_BRAIN_RECOVERY_ENABLED": "1",
                "AI_BRAIN_RECOVERY_ORGANIZATION_IDS": str(self.organization.pk)}):
            with patch.object(recovery, "_resolution", side_effect=invalid_read):
                result = recovery.assess_evidence(state)
        self.assertEqual(result["evidence_coverage"].status, "storage_error")
        self.assertTrue(Organization.objects.filter(pk=self.organization.pk).exists())

    def test_command_defaults_to_preflight(self):
        out = io.StringIO()
        with patch.object(evaluation, "evaluate", side_effect=AssertionError("No live call")):
            call_command("evaluate_ai_recovery", organization_id=self.organization.pk, stdout=out)
        self.assertEqual(json.loads(out.getvalue())["mode"], "read_only_preflight")

    def test_command_needs_explicit_live_consent(self):
        with self.assertRaises(CommandError):
            call_command("evaluate_ai_recovery", organization_id=self.organization.pk, scenarios="unused.json")
        with self.assertRaises(CommandError):
            call_command("evaluate_ai_recovery", organization_id=self.organization.pk, live=True)

    def test_report_file_is_private_and_not_overwritten(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            call_command("evaluate_ai_recovery", organization_id=self.organization.pk,
                         output=str(output), stdout=io.StringIO())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(CommandError):
                call_command("evaluate_ai_recovery", organization_id=self.organization.pk,
                             output=str(output), stdout=io.StringIO())

    def test_actual_playground_replay_does_not_write_crm_or_queue_messages(self):
        from apps.channels.models import WhatsAppMessage
        from apps.channels.instagram_models import InstagramMessage
        before = (Lead.objects.count(), LeadReminder.objects.count(),
                  WhatsAppMessage.objects.count(), InstagramMessage.objects.count())
        decision = EngagementDecision(should_engage=True, message="Approved details",
            file_document_id=None, crm_actions=[], reason="NORMAL_CONVERSATION", model="recorded-test")
        with (
            patch("apps.ai_engagement.services.engagement.EngagementService.engage", return_value=decision),
            patch("apps.ai_engagement.services.phase5_6_runtime.sandbox_evidence_context", return_value=nullcontext()),
            patch("apps.ai_engagement.services.playground.apply_first_inbound_welcome", side_effect=lambda **kw: kw["decision"]),
            patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text", side_effect=AssertionError("No live model")),
            patch("celery.app.task.Task.apply_async", side_effect=AssertionError("No message publication")),
        ):
            report = evaluation.evaluate(self.organization, scenario())
        self.assertTrue(report["comparison_valid"], report)
        self.assertEqual(report["completed_turns"], 2)
        self.assertEqual(before, (Lead.objects.count(), LeadReminder.objects.count(),
                                 WhatsAppMessage.objects.count(), InstagramMessage.objects.count()))
