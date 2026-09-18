from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from django.db import close_old_connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from apps.ai_engagement.models import AIActionReceipt, OrgInfo
from apps.ai_engagement.services.crm_executor import CRMActionExecutionError, CRMActionExecutor
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.evidence_resolver import (
    EvidenceItem, EvidenceResolution, GroundingCategory, InformationClass,
)
from apps.ai_engagement.services.grounding_validation import exact_evidence_reply
from apps.ai_engagement.services.phase5_6_safety_fixes import _extractive_evidence_match
from apps.ai_engagement.services.phase7_completion_runtime import _deterministically_supported_reply
from apps.ai_engagement.tests import test_engagement_controls as control_fixtures
from apps.crm.models import Lead, LeadNote, LeadReminder


def resolution(content, kind="pricing"):
    return EvidenceResolution(
        category=GroundingCategory.STRUCTURED_ORG_DATA,
        information_class=InformationClass.STATIC_CONFIGURED,
        question_type=kind, sensitive=True, verified=True,
        evidence=(EvidenceItem(source_id="org:verified", source_type="organization_runtime_profile", content=content),),
    )


def decision(message):
    return EngagementDecision(
        should_engage=True, message=message, file_document_id=None, crm_actions=[],
        qualification_updates=[], reason="ANSWER_ORG_QUESTION",
        reason_code="ANSWER_ORG_QUESTION", model="test",
    )


class GroundingProofRegressionTests(SimpleTestCase):
    def test_all_shortcuts_reject_changed_numbers_polarity_conditions_and_extra_claims(self):
        cases = [
            ("Price is 99 per month", "Price is 10 per month", "pricing"),
            ("Refunds are not available", "Refunds are available", "policy"),
            ("Refunds are available only within 7 days", "Refunds are available", "policy"),
            ("Basic costs 99. Pro costs 199.", "Basic costs 199. Pro costs 99.", "pricing"),
            ("₹999/month", "Our price is ₹999/month. A 50% discount is guaranteed.", "pricing"),
            ("Price is $99 per month", "Price is ₹99 per month", "pricing"),
            ("Price is 99 per year", "Price is 99 per month", "pricing"),
            ("रिफंड उपलब्ध नहीं है", "रिफंड उपलब्ध है", "policy"),
        ]
        for evidence, reply, kind in cases:
            with self.subTest(evidence=evidence, reply=reply):
                for predicate in (exact_evidence_reply, _extractive_evidence_match, _deterministically_supported_reply):
                    self.assertFalse(predicate(decision(reply), resolution(evidence, kind)))

    def test_complete_evidence_can_skip_verifier_without_changing_numbers_or_conditions(self):
        text = "Refunds are not available after 7 days."
        self.assertTrue(exact_evidence_reply(decision(text), resolution(text, "policy")))
        self.assertTrue(_extractive_evidence_match(decision("Our price is ₹999/month."), resolution("₹999/month")))


class InstalledGroundingRegressionTests(TestCase):
    setUp = control_fixtures.AIEngagementControlTests.setUp

    def test_installed_graph_guard_does_not_short_circuit_on_unsupported_claims(self):
        from apps.ai_engagement.graph import workflow
        from apps.ai_engagement.services import phase5_6_runtime
        for evidence, reply, kind in [
            ("Price is 99 per month", "Price is 10 per month", "pricing"),
            ("Refunds are not available", "Refunds are available", "policy"),
            ("₹999/month", "Our price is ₹999/month. Guaranteed 50% discount.", "pricing"),
        ]:
            with self.subTest(reply=reply):
                token = phase5_6_runtime._ACTIVE_EVIDENCE.set({
                    "organization_id": str(self.organization.id), "lead_id": str(self.lead.id),
                    "resolution": resolution(evidence, kind),
                })
                context = SimpleNamespace(
                    conversation={"messages": []}, organization={"about": "", "name": "Acme", "engagement_instructions": "", "bot_languages": "English"},
                    knowledge=[], lead={"attributes": {}},
                )
                try:
                    with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
                        provider.return_value.generate_text.return_value = SimpleNamespace(text='{"approved": false, "reason": "unsupported_claim"}')
                        output = workflow.check_grounding({
                            "decision": decision(reply), "organization": self.organization,
                            "lead": self.lead, "context": context, "latest_text": "What is your price?",
                            "runtime_policy": {}, "qualification_state": {}, "requirements": [],
                        })
                    provider.assert_called_once()
                    self.assertFalse(output["grounding_approved"])
                    self.assertEqual(output["decision"].reason_code, "UNKNOWN_INFORMATION")
                finally:
                    phase5_6_runtime._ACTIVE_EVIDENCE.reset(token)


class ActionReceiptRegressionTests(TestCase):
    setUp = control_fixtures.AIEngagementControlTests.setUp
    _inbound = control_fixtures.AIEngagementControlTests._inbound

    def _execute(self, source, actions=None):
        return CRMActionExecutor().execute(
            organization=self.organization, lead=self.lead, source_message=source,
            actions=actions or [{"type": "add_note", "note": "Source-bound callback request"}],
        )

    def test_public_wrapped_executor_accepts_source_and_replays_same_result(self):
        source = self._inbound("receipt-note")
        first = self._execute(source)
        second = self._execute(source)
        self.assertEqual(first, second)
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Source-bound callback request").count(), 1)
        self.assertEqual(AIActionReceipt.objects.filter(organization=self.organization, lead=self.lead).count(), 1)

    def test_different_messages_are_distinct_actions(self):
        self._execute(self._inbound("receipt-first"))
        self._execute(self._inbound("receipt-second"))
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Source-bound callback request").count(), 2)

    def test_duplicate_proposals_in_same_plan_are_coalesced(self):
        source = self._inbound("receipt-duplicate-in-plan")
        action = {"type": "add_note", "note": "Same planned note"}
        output = self._execute(source, [action, action])
        self.assertEqual(output[0]["note_id"], output[1]["note_id"])
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Same planned note").count(), 1)

    def test_reminder_retries_do_not_create_duplicate_reminders(self):
        source = self._inbound("receipt-reminder")
        actions = [{"type": "create_reminder", "title": "Callback", "description": "Requested explicitly", "due_at": "2026-10-01T10:00:00+05:30"}]
        self.assertEqual(self._execute(source, actions), self._execute(source, actions))
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead, title="Callback").count(), 1)

    def test_failed_batch_rolls_back_receipts_and_crm_mutations(self):
        source = self._inbound("receipt-rollback")
        actions = [
            {"type": "add_note", "note": "Rollback note"},
            {"type": "create_reminder", "title": "Callback", "description": "Requested", "due_at": "2026-10-01T10:00:00+05:30"},
        ]
        with patch.object(CRMActionExecutor, "_execute_create_reminder", side_effect=CRMActionExecutionError("temporary failure")):
            with self.assertRaises(CRMActionExecutionError):
                self._execute(source, actions)
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead).exists())
        self.assertFalse(LeadNote.objects.filter(lead=self.lead, note="Rollback note").exists())
        self._execute(source, actions)
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Rollback note").count(), 1)

    def test_fresh_permission_revocation_prevents_new_mutation(self):
        source = self._inbound("receipt-disabled")
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_enabled = False
        info.save(update_fields=["ai_enabled"])
        with self.assertRaises(CRMActionExecutionError):
            self._execute(source)
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead).exists())

    def test_unbound_source_identifier_is_rejected(self):
        source = self._inbound("receipt-source")
        other = Lead.objects.create(organization=self.organization, pipeline=self.pipeline, stage=self.new_lead, name="Other", phone="+919111111112")
        with self.assertRaises(CRMActionExecutionError):
            CRMActionExecutor().execute(organization=self.organization, lead=other, source_message=source, actions=[{"type": "add_note", "note": "Must not happen"}])
        self.assertFalse(LeadNote.objects.filter(lead=other, note="Must not happen").exists())


class ConcurrentActionReceiptTests(TransactionTestCase):
    setUp = control_fixtures.AIEngagementControlTests.setUp
    _inbound = control_fixtures.AIEngagementControlTests._inbound

    def test_postgresql_concurrent_workers_create_one_note(self):
        from apps.channels.models import WhatsAppMessage
        source = self._inbound("receipt-concurrent")
        barrier = Barrier(2)
        def worker():
            close_old_connections()
            try:
                lead = Lead.objects.select_related("organization").get(pk=self.lead.pk)
                message = WhatsAppMessage.objects.get(pk=source.pk)
                barrier.wait(timeout=15)
                return CRMActionExecutor().execute(
                    organization=lead.organization, lead=lead, source_message=message,
                    actions=[{"type": "add_note", "note": "Concurrent note"}],
                )[0]["note_id"]
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Concurrent note").count(), 1)
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 1)
