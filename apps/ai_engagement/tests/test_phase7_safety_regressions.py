from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from django.db import close_old_connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, skipUnlessDBFeature

from apps.ai_engagement.models import CRMActionReceipt, OrgInfo
from apps.ai_engagement.services import phase5_6_runtime, trace_service
from apps.ai_engagement.services.crm_executor import CRMActionExecutionError, CRMActionExecutor
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.evidence_resolver import (
    EvidenceItem, EvidenceResolution, GroundingCategory, InformationClass,
)
from apps.ai_engagement.services.grounding_validation import (
    is_social_only_reply, matches_verified_evidence,
)
from apps.ai_engagement.services.phase5_6_safety_fixes import (
    _extractive_evidence_match, _working_hours_question,
)
from apps.ai_engagement.services.phase7_completion_runtime import _deterministically_supported_reply
from apps.ai_engagement.tests import test_engagement_controls as fixtures
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead, LeadNote, LeadReminder


COUNTEREXAMPLES = (
    ("Price is 99 per month", "Price is 10 per month"),
    ("Refunds are not available", "Refunds are available"),
    ("Basic costs 99; Pro costs 199", "Basic costs 199; Pro costs 99"),
    ("Refunds require approval", "Refunds require approval and are guaranteed"),
    ("₹999/month", "Our price is ₹999/month. A 50% discount is guaranteed."),
    ("Price is $99 per month", "Price is €99 per month"),
    ("Price is $99 per month", "Price is $99 per year"),
    ("वापसी उपलब्ध नहीं है।", "वापसी उपलब्ध है।"),
    ("Refund nahi milega", "Refund milega"),
)


def resolution(content):
    return EvidenceResolution(
        category=GroundingCategory.STRUCTURED_ORG_DATA,
        information_class=InformationClass.STATIC_CONFIGURED,
        question_type="pricing", sensitive=True, verified=True,
        evidence=(EvidenceItem(source_id="org:fact", source_type="organization_runtime_profile", content=content),),
    )


def decision(message, **overrides):
    return EngagementDecision(**{
        "should_engage": True, "message": message, "file_document_id": None,
        "crm_actions": [], "qualification_updates": [], "reason": "ANSWER_ORG_QUESTION",
        "reason_code": "ANSWER_ORG_QUESTION", "model": "test", **overrides,
    })


class ConservativeGroundingTests(SimpleTestCase):
    def test_changed_assertions_never_take_either_shortcut(self):
        for evidence, reply in COUNTEREXAMPLES:
            with self.subTest(evidence=evidence, reply=reply):
                self.assertFalse(_deterministically_supported_reply(decision(reply), resolution(evidence)))
                self.assertFalse(_extractive_evidence_match(decision(reply), resolution(evidence)))

    def test_full_assertion_match_and_narrow_scalar_template_remain_cheap(self):
        self.assertTrue(matches_verified_evidence(decision("Refunds are not available"), resolution("Refunds are not available")))
        self.assertTrue(_extractive_evidence_match(decision("Our price is ₹999/month."), resolution("₹999/month")))
        self.assertFalse(matches_verified_evidence(decision("Our price is ₹999/month."), resolution("₹999/month")))

    def test_correct_fragment_is_not_a_complete_policy(self):
        self.assertFalse(_extractive_evidence_match(
            decision("Refunds are available"), resolution("Refunds are available only within 7 days"),
        ))

    def test_social_allowlist_does_not_approve_business_claims(self):
        self.assertTrue(is_social_only_reply(decision("Thanks for sharing that.", reason_code="NORMAL_CONVERSATION"), None))
        self.assertFalse(is_social_only_reply(decision("We deliver worldwide.", reason_code="NORMAL_CONVERSATION"), None))
        self.assertFalse(is_social_only_reply(decision("Your order has shipped.", reason_code="NORMAL_CONVERSATION"), None))

    def test_open_appointment_slots_are_not_working_hours(self):
        self.assertFalse(_working_hours_question("Any open appointment slots during business hours tomorrow?"))
        self.assertFalse(_working_hours_question("Are you available at 3 PM?"))
        self.assertTrue(_working_hours_question("What are your working hours?"))

    def test_side_effects_cannot_bypass_grounding(self):
        self.assertFalse(matches_verified_evidence(
            decision("₹999/month", crm_actions=[{"type": "add_note", "note": "x"}]), resolution("₹999/month"),
        ))


class InstalledGroundingTests(TestCase):
    setUp = fixtures.AIEngagementControlTests.setUp

    def test_installed_guard_chain_sends_counterexamples_to_existing_verifier(self):
        from apps.ai_engagement.graph import evidence as graph_evidence

        context = SimpleNamespace(
            conversation={"messages": []}, knowledge=[], lead={"attributes": {}},
            organization={"about": "", "name": "Acme", "engagement_instructions": "", "bot_languages": ""},
        )
        for evidence, reply in COUNTEREXAMPLES:
            with self.subTest(reply=reply):
                token = phase5_6_runtime._ACTIVE_EVIDENCE.set({
                    "organization_id": str(self.organization.id), "lead_id": str(self.lead.id),
                    "resolution": resolution(evidence),
                })
                try:
                    state = {
                        "decision": decision(reply), "organization": self.organization, "lead": self.lead,
                        "context": context, "latest_text": "What is your policy or price?",
                        "runtime_policy": {}, "qualification_state": {}, "requirements": [],
                    }
                    with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
                        provider.return_value.generate_text.return_value = SimpleNamespace(
                            text='{"approved": false, "reason": "unsupported_fact"}',
                        )
                        result = graph_evidence.check_grounding(state)
                    provider.assert_called_once()
                    self.assertFalse(result["grounding_approved"])
                    self.assertEqual(result["decision"].reason_code, "UNKNOWN_INFORMATION")
                finally:
                    phase5_6_runtime._ACTIVE_EVIDENCE.reset(token)


class ActionReceiptTests(TestCase):
    _inbound = fixtures.AIEngagementControlTests._inbound

    def setUp(self):
        fixtures.AIEngagementControlTests.setUp(self)
        OrgInfo.objects.update_or_create(organization=self.organization, defaults={"ai_enabled": True})
        self.source = self._inbound("receipt-source")

    def execute(self, actions, source=None):
        return CRMActionExecutor().execute(
            organization=self.organization, lead=self.lead, actions=actions,
            source_message=source or self.source,
        )

    def test_source_argument_survives_all_installed_wrappers(self):
        result = self.execute([{"type": "add_note", "note": "Wrapper compatibility"}])
        self.assertEqual(result[0]["status"], "executed")
        self.assertEqual(CRMActionReceipt.objects.filter(organization=self.organization, lead=self.lead).count(), 1)

    def test_replayed_note_returns_original_result_once(self):
        actions = [{"type": "add_note", "note": "Idempotent note"}]
        first = self.execute(actions)
        second = self.execute(actions)
        self.assertEqual(first, second)
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Idempotent note").count(), 1)
        self.assertEqual(CRMActionReceipt.objects.filter(organization=self.organization, lead=self.lead).count(), 1)

    def test_replayed_reminder_is_not_created_twice(self):
        actions = [{"type": "create_reminder", "title": "Idempotent callback", "description": "Customer requested", "due_at": "2030-01-01T10:00:00+05:30"}]
        self.assertEqual(self.execute(actions), self.execute(actions))
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead, title="Idempotent callback").count(), 1)

    def test_new_source_can_request_the_same_action_again(self):
        actions = [{"type": "add_note", "note": "Separate legitimate request"}]
        first = self.execute(actions)
        second = self.execute(actions, source=self._inbound("receipt-second-source"))
        self.assertNotEqual(first[0]["note_id"], second[0]["note_id"])
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Separate legitimate request").count(), 2)

    def test_receipt_failure_rolls_back_mutation(self):
        with patch("apps.ai_engagement.models.CRMActionReceipt.objects.create", side_effect=RuntimeError("receipt persistence unavailable")):
            with self.assertRaises(RuntimeError):
                self.execute([{"type": "add_note", "note": "Must roll back"}])
        self.assertFalse(LeadNote.objects.filter(lead=self.lead, note="Must roll back").exists())
        self.assertFalse(CRMActionReceipt.objects.filter(organization=self.organization, lead=self.lead).exists())

    def test_entire_action_batch_rolls_back_on_later_failure(self):
        actions = [
            {"type": "add_note", "note": "Batch rollback"},
            {"type": "create_reminder", "title": "Fail later", "description": "x", "due_at": "2030-01-01T10:00:00+05:30"},
        ]
        with patch.object(CRMActionExecutor, "_execute_create_reminder", side_effect=CRMActionExecutionError("write failed")):
            with self.assertRaises(CRMActionExecutionError):
                self.execute(actions)
        self.assertFalse(LeadNote.objects.filter(lead=self.lead, note="Batch rollback").exists())
        self.assertFalse(CRMActionReceipt.objects.filter(organization=self.organization, lead=self.lead).exists())

    def test_current_permission_denial_prevents_mutation(self):
        OrgInfo.objects.filter(organization=self.organization).update(ai_enabled=False)
        with self.assertRaises(CRMActionExecutionError):
            self.execute([{"type": "add_note", "note": "Forbidden after toggle"}])
        self.assertFalse(LeadNote.objects.filter(lead=self.lead, note="Forbidden after toggle").exists())

    def test_another_leads_message_cannot_authorize_actions(self):
        other = Lead.objects.create(
            organization=self.organization, pipeline=self.pipeline, stage=self.new_lead,
            name="Other", phone="+919222222222",
        )
        source = WhatsAppMessage.objects.create(
            organization=self.organization, account=self.account, lead=other,
            direction="inbound", external_id="other-lead-source", body="Hello", status="received",
        )
        with self.assertRaises(CRMActionExecutionError):
            self.execute([{"type": "add_note", "note": "Foreign evidence"}], source=source)
        self.assertFalse(LeadNote.objects.filter(lead=self.lead, note="Foreign evidence").exists())

    def test_trace_bound_source_is_used_without_newer_message_substitution(self):
        token = trace_service.begin_trace(organization=self.organization, lead=self.lead, source_message=self.source, account=self.account)
        try:
            newer = self._inbound("receipt-newer-source")
            CRMActionExecutor().execute(
                organization=self.organization, lead=self.lead,
                actions=[{"type": "add_note", "note": "Bound to original source"}],
            )
        finally:
            trace_service._CURRENT.reset(token)
        receipt = CRMActionReceipt.objects.get(organization=self.organization, lead=self.lead)
        self.assertEqual(receipt.source_inbound_message_id, self.source.id)
        self.assertNotEqual(receipt.source_inbound_message_id, newer.id)

    def test_trace_failure_does_not_break_replay(self):
        actions = [{"type": "add_note", "note": "Trace-safe receipt"}]
        first = self.execute(actions)
        with patch("apps.ai_engagement.services.trace_service.append", side_effect=RuntimeError("trace unavailable")):
            self.assertEqual(first, self.execute(actions))
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Trace-safe receipt").count(), 1)


class ConcurrentActionReceiptTests(TransactionTestCase):
    _inbound = fixtures.AIEngagementControlTests._inbound

    def setUp(self):
        fixtures.AIEngagementControlTests.setUp(self)
        OrgInfo.objects.update_or_create(organization=self.organization, defaults={"ai_enabled": True})
        self.source = self._inbound("concurrent-receipt-source")

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_workers_create_only_one_note_and_receipt(self):
        barrier = Barrier(2)
        organization_id, lead_id, source_id = self.organization.id, self.lead.id, self.source.id

        def worker():
            close_old_connections()
            try:
                lead = Lead.objects.select_related("organization", "pipeline", "stage").get(pk=lead_id, organization_id=organization_id)
                source = WhatsAppMessage.objects.select_related("account", "lead").get(pk=source_id, organization_id=organization_id, lead_id=lead_id)
                barrier.wait(timeout=15)
                return CRMActionExecutor().execute(
                    organization=lead.organization, lead=lead, source_message=source,
                    actions=[{"type": "add_note", "note": "Concurrent exactly-once note"}],
                )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(LeadNote.objects.filter(lead_id=lead_id, note="Concurrent exactly-once note").count(), 1)
        self.assertEqual(CRMActionReceipt.objects.filter(organization_id=organization_id, lead_id=lead_id).count(), 1)
