import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.evaluation.runner import summarize_junit
from apps.ai_engagement.evaluation.scenarios import load_scenarios
from apps.ai_engagement.models import Chunk, Document, LeadSignal, OrgInfo
from apps.ai_engagement.services import phase5_6_runtime as evidence_runtime
from apps.ai_engagement.services import conversation_policy_runtime as policy_runtime
from apps.ai_engagement.services import intent_runtime
from apps.ai_engagement.services.action_planner import ActionPlanner
from apps.ai_engagement.services.evidence_resolver import EvidenceResolution, GroundingCategory, InformationClass, EvidenceResolver
from apps.ai_engagement.services.intent_types import Intent, IntentDecision
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_execution_contract import resolve_before_generation
from apps.ai_engagement.services.qualification_state import record_last_asked_requirement
from apps.ai_engagement.services.turn_scope import isolated_turn
from apps.ai_engagement.services.trace_sanitizer import sanitize
from apps.ai_engagement.tests import test_engagement_controls as fixtures
from apps.crm.models import AttributeDefinition
from apps.organizations.models import Organization


class PhaseIntegrationTests(TestCase):
    setUp = fixtures.AIEngagementControlTests.setUp
    _inbound = fixtures.AIEngagementControlTests._inbound

    def source(self, body, key="integration-source"):
        source = self._inbound(key)
        source.body = body
        source.save(update_fields=["body"])
        return source

    def configure_compound(self, enabled):
        self.organization.settings = {"ai_qualification": {"capture_multiple_answers": enabled}}
        self.organization.save(update_fields=["settings"])
        for key in ("tool", "volume"):
            AttributeDefinition.objects.create(organization=self.organization, name=key, key=key)
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = ("[id: tool] Which tool do you use?\nA. Excel\nB. CRM\n"
                                           "[id: volume] How many leads do you receive daily?\nAll questions are required")
        info.engagement_instructions = "## Attribute mapped\ntool -> tool\nvolume -> volume"
        info.save()
        requirements = compile_qualification_requirements(info.qualification_requirements)["requirements"]
        record_last_asked_requirement(self.lead, requirements[0]["id"], requirements=requirements)
        return requirements

    def test_compound_capture_is_opt_in_and_preserves_existing_default(self):
        self.configure_compound(False)
        source = self.source("I use Excel and receive 25 leads daily.")
        resolve_before_generation(organization=self.organization, lead=self.lead, source_message_id=source.pk)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["tool"], "Excel")
        self.assertNotIn("volume", self.lead.attributes)

    def test_compound_capture_does_not_use_unrelated_budget_for_lead_volume(self):
        self.configure_compound(True)
        source = self.source("I use Excel and my budget is 25000.")
        resolve_before_generation(organization=self.organization, lead=self.lead, source_message_id=source.pk)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["tool"], "Excel")
        self.assertNotIn("volume", self.lead.attributes)

    def test_semantic_evidence_reuses_real_same_tenant_chunk_without_provider(self):
        doc = Document.objects.create(organization=self.organization, name="Local prices", source_key="local", processing_status="completed")
        chunk = Chunk.objects.create(organization=self.organization, document=doc, content="Price 99 per month")
        resolution = EvidenceResolution(category=GroundingCategory.NO_VERIFIED_EVIDENCE,
            information_class=InformationClass.UNKNOWN, question_type="pricing", sensitive=True, verified=False)
        context = SimpleNamespace(organization={"id": str(self.organization.pk)}, lead={"id": str(self.lead.pk)},
                                  knowledge=[{"chunk_id": chunk.pk, "similarity": 0.9, "content": "UNTRUSTED INJECTED TEXT"}])
        token = evidence_runtime._ACTIVE_EVIDENCE.set({"organization_id": str(self.organization.pk), "lead_id": str(self.lead.pk), "resolution": resolution})
        try:
            with patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text", side_effect=AssertionError("No extra model")):
                refined = evidence_runtime.refine_evidence_from_context(context=context, resolution=resolution)
            self.assertTrue(refined.verified)
            self.assertEqual(refined.evidence[0].content, chunk.content)
            self.assertEqual(refined.evidence[0].metadata["chunk_id"], chunk.pk)
        finally:
            evidence_runtime._ACTIVE_EVIDENCE.reset(token)

    def test_foreign_semantic_hit_cannot_be_adopted_even_with_high_score(self):
        foreign = Organization.objects.create(name="Other")
        doc = Document.objects.create(organization=foreign, name="Private", processing_status="completed")
        chunk = Chunk.objects.create(organization=foreign, document=doc, content="Private price 7")
        resolution = EvidenceResolution(category=GroundingCategory.NO_VERIFIED_EVIDENCE,
            information_class=InformationClass.UNKNOWN, question_type="pricing", sensitive=True, verified=False)
        context = SimpleNamespace(organization={"id": str(self.organization.pk)}, lead={"id": str(self.lead.pk)}, knowledge=[{"chunk_id": chunk.pk, "similarity": 1.0}])
        token = evidence_runtime._ACTIVE_EVIDENCE.set({"organization_id": str(self.organization.pk), "lead_id": str(self.lead.pk), "resolution": resolution})
        try:
            self.assertFalse(evidence_runtime.refine_evidence_from_context(context=context, resolution=resolution).verified)
        finally:
            evidence_runtime._ACTIVE_EVIDENCE.reset(token)

    def test_objection_approved_offer_is_evidence_only_for_matching_organization(self):
        self.organization.settings = {"ai_objections": {"categories": {"PRICE_TOO_HIGH": {
            "approved_facts": ["Monthly payment is available."], "discount": "An approved 5% annual discount is available."}}}}
        resolution = EvidenceResolver().resolve(organization=self.organization, lead=self.lead, question="Too expensive",
                                              intent_decision=IntentDecision(primary_intent=Intent.OBJECTION))
        self.assertTrue(resolution.verified)
        self.assertEqual(len(resolution.evidence), 2)
        self.assertTrue(all(str(self.organization.pk) in item.source_id for item in resolution.evidence))

    def test_configured_escalation_is_proposal_only_and_cannot_override_opt_out(self):
        self.organization.settings = {"ai_objections": {"categories": {"TRUST_CONCERN": {"escalate": True}}}}
        self.organization.save(update_fields=["settings"])
        decision = SimpleNamespace(crm_actions=[], file_document_id=None, reason_code="NORMAL_CONVERSATION")
        source = self.source("Is this a scam?")
        plan = ActionPlanner().plan(organization=self.organization, lead=self.lead, source_message=source, decision=decision)
        self.assertEqual([item.action_type for item in plan.actions], ["HUMAN_HANDOFF"])
        self.assertEqual(plan.executor_actions, [])
        source = self.source("Stop messaging me. Is this a scam?", "optout-escalation")
        plan = ActionPlanner().plan(organization=self.organization, lead=self.lead, source_message=source, decision=decision)
        self.assertFalse(plan.actions)

    def test_opt_out_signal_is_saved_once_even_when_no_reply_is_permitted(self):
        source = self.source("Stop messaging me", "early-optout")
        source.save(update_fields=["body"])
        self.assertEqual(LeadSignal.objects.filter(organization=self.organization, lead=self.lead,
                         source_message_id=source.pk, kind="opt_out").count(), 1)

    def test_disabled_signal_collection_does_not_store_optout_observation(self):
        self.organization.settings = {"ai_signals": {"enabled": False}}
        self.organization.save(update_fields=["settings"])
        source = self.source("Stop messaging me", "disabled-optout")
        self.assertFalse(LeadSignal.objects.filter(source_message_id=source.pk).exists())


class PhaseScopeAndRunnerTests(SimpleTestCase):
    def test_task_context_is_cleared_on_entry_and_restored_even_on_error(self):
        old_intent = intent_runtime._CURRENT.set({"organization_id": "previous"})
        old_turn = policy_runtime._TURN.set({"organization_id": "previous"})
        try:
            with self.assertRaises(RuntimeError):
                with isolated_turn():
                    self.assertIsNone(intent_runtime._CURRENT.get())
                    self.assertIsNone(policy_runtime._TURN.get())
                    policy_runtime._TURN.set({"organization_id": "new"})
                    raise RuntimeError("test")
            self.assertEqual(policy_runtime._TURN.get()["organization_id"], "previous")
        finally:
            policy_runtime._TURN.reset(old_turn)
            intent_runtime._CURRENT.reset(old_intent)

    def test_trace_redacts_credentials_inside_free_text(self):
        result = sanitize({"message": "password=supersecret api_key=privatevalue Authorization: Bearer abcdefgh123456"})
        self.assertNotIn("supersecret", str(result))
        self.assertNotIn("privatevalue", str(result))
        self.assertNotIn("abcdefgh123456", str(result))

    def test_only_exact_backend_question_suffix_is_exempt_from_internal_label_filter(self):
        from apps.ai_engagement.services.qualification_execution_policy_guard import _strip_protected_generated_text
        state = {"protected_configuration_labels": ["tool", "internal_config"],
                 "customer_next_requirement": {"question": "Which tool?\nA. Excel\nB. CRM"}}
        text = "This is the supported answer.\n\nWhich tool?\nA. Excel\nB. CRM"
        self.assertEqual(_strip_protected_generated_text(text, state), text)
        self.assertNotIn("internal_config", _strip_protected_generated_text("Hi.\n\ninternal_config", {**state, "response_plan": {}}))

    def test_scenario_loader_rejects_duplicates_and_oversized_input(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "scenario.json"
            scenario = {"id": "duplicate", "turns": [{"message": "Hello", "expect": {}}]}
            target.write_text(json.dumps({"version": 1, "scenarios": [scenario, scenario]}))
            with self.assertRaises(ValueError):
                load_scenarios(target)
            target.write_text("x" * 1_000_001)
            with self.assertRaises(ValueError):
                load_scenarios(target)

    def test_report_categorizes_failures_and_fails_closed_on_missing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.xml"
            target.write_text('<testsuites><testsuite><testcase name="ok"/><testcase name="bad"><failure message="[tenant_isolation] foreign hit"/></testcase></testsuite></testsuites>')
            result = summarize_junit(target, returncode=1)
            self.assertEqual(result["passed"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["failures_by_category"]["tenant_isolation"], 1)
            self.assertFalse(result["live_model_evaluated"])
            self.assertEqual(summarize_junit(target.with_suffix(".missing"), returncode=5)["failed"], 1)

    def test_evaluation_command_uses_fixed_test_settings_and_blocks_failed_checks(self):
        report = {"passed": 1, "failed": 1, "skipped": 0, "failures_by_category": {"actions": 1}}
        with patch("apps.ai_engagement.management.commands.evaluate_ai.run_evaluation", return_value=report) as run:
            with self.assertRaises(CommandError):
                call_command("evaluate_ai", output="/tmp/unused-test-report.json", verbosity=0)
        self.assertEqual(run.call_args.kwargs["settings_module"], "config.settings.ai_evaluation")


    def test_evaluation_settings_never_inherit_live_cache_queues_or_test_database_name(self):
        from importlib import import_module

        isolated = import_module("config.settings.ai_evaluation")
        self.assertIn("LocMem", isolated.CACHES["default"]["BACKEND"])
        self.assertIn("InMemory", isolated.CHANNEL_LAYERS["default"]["BACKEND"])
        self.assertEqual(isolated.CELERY_BROKER_URL, "memory://")
        self.assertEqual(isolated.CELERY_RESULT_BACKEND, "cache+memory://")
        self.assertEqual(isolated.REDIS_URL, "")
        self.assertEqual(isolated.OPENAI_API_KEY, "")
        for database in isolated.DATABASES.values():
            self.assertTrue(database["TEST"]["NAME"].startswith("test_shvya_ai_eval_"))
            # Django changes NAME to TEST.NAME while executing this suite.
            from config.settings import base
            self.assertNotEqual(database["TEST"]["NAME"], base.DATABASES["default"]["NAME"])
