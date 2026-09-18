from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase

from apps.ai_engagement.models import AIActionReceipt
from apps.ai_engagement.services.crm_executor import CRMActionExecutor, CRMActionExecutionError
from apps.ai_engagement.services import conversation_policy_runtime
from apps.ai_engagement.services.tenant_guard import TenantScopeError
from apps.ai_engagement.tests import test_engagement_controls as control_fixtures
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead, LeadNote, LeadReminder
from apps.organizations.models import Organization


class ActionReceiptTests(TestCase):
    setUp = control_fixtures.AIEngagementControlTests.setUp
    _inbound = control_fixtures.AIEngagementControlTests._inbound

    def execute(self, source, actions=None):
        return CRMActionExecutor().execute(
            organization=self.organization, lead=self.lead, source_message=source,
            actions=actions or [{"type": "add_note", "note": "Customer requested a callback."}],
        )

    def test_fully_installed_wrappers_accept_source_and_retry_does_not_duplicate_note(self):
        source = self._inbound("receipt-note")
        first = self.execute(source)
        second = self.execute(source)
        self.assertEqual(first[0]["note_id"], second[0]["note_id"])
        self.assertTrue(second[0]["idempotent_replay"])
        self.assertEqual(LeadNote.objects.filter(pk=first[0]["note_id"]).count(), 1)
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 1)

    def test_retry_and_duplicate_batch_create_one_reminder(self):
        source = self._inbound("receipt-reminder")
        action = {"type": "create_reminder", "title": "Callback", "description": "Requested time",
                  "due_at": "2026-09-20T10:00:00+05:30"}
        first = self.execute(source, [action, action])
        second = self.execute(source, [action])
        self.assertEqual(first[0]["reminder_id"], first[1]["reminder_id"])
        self.assertEqual(first[0]["reminder_id"], second[0]["reminder_id"])
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)

    def test_different_messages_are_independent_operations(self):
        self.execute(self._inbound("receipt-independent-a"))
        self.execute(self._inbound("receipt-independent-b"))
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 2)

    def test_receipt_and_mutation_roll_back_together(self):
        source = self._inbound("receipt-rollback")
        before = LeadNote.objects.filter(lead=self.lead).count()
        with patch.object(AIActionReceipt.objects, "create", side_effect=RuntimeError("storage unavailable")):
            with self.assertRaises(RuntimeError):
                self.execute(source)
        self.assertEqual(LeadNote.objects.filter(lead=self.lead).count(), before)
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead).exists())
        self.execute(source)
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 1)

    def test_invalid_or_foreign_source_never_creates_receipt(self):
        source = self._inbound("receipt-foreign")
        other = Organization.objects.create(name="Foreign")
        WhatsAppMessage.objects.filter(pk=source.pk).update(organization=other)
        source.refresh_from_db()
        with self.assertRaises((CRMActionExecutionError, TenantScopeError)):
            self.execute(source)
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead).exists())

    def test_live_turn_recovers_source_even_when_existing_caller_omits_argument(self):
        source = self._inbound("receipt-provenance")
        token = conversation_policy_runtime._TURN.set({
            "organization_id": str(self.organization.pk), "lead_id": str(self.lead.pk),
            "source_message_id": str(source.pk),
        })
        try:
            self.execute(None)
            self.execute(None)
        finally:
            conversation_policy_runtime._TURN.reset(token)
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 1)

    def test_permission_revocation_is_rechecked_under_lock(self):
        source = self._inbound("receipt-permission")
        from apps.ai_engagement.services.action_planner import ActionPlanner
        original = ActionPlanner.plan

        def change_permissions(planner, **kwargs):
            plan = original(planner, **kwargs)
            Organization.objects.filter(pk=self.organization.pk).update(
                settings={"ai_action_permissions": {"allowed_action_types": []}})
            return plan

        with patch.object(ActionPlanner, "plan", change_permissions):
            with self.assertRaises(CRMActionExecutionError):
                self.execute(source)
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead).exists())


class ConcurrentActionReceiptTests(TransactionTestCase):
    setUp = control_fixtures.AIEngagementControlTests.setUp
    _inbound = control_fixtures.AIEngagementControlTests._inbound

    def test_concurrent_same_source_creates_one_note_on_postgresql(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires real PostgreSQL row-lock behavior")
        source = self._inbound("receipt-concurrent")
        barrier = Barrier(2)
        org_id, lead_id, source_id = self.organization.pk, self.lead.pk, source.pk

        def execute():
            close_old_connections()
            try:
                org = Organization.objects.get(pk=org_id)
                lead = Lead.objects.select_related("organization", "pipeline", "stage").get(pk=lead_id)
                inbound = WhatsAppMessage.objects.get(pk=source_id)
                barrier.wait(timeout=10)
                return CRMActionExecutor().execute(
                    organization=org, lead=lead, source_message=inbound,
                    actions=[{"type": "add_note", "note": "Concurrent source event"}],
                )[0]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: execute(), range(2)))
        self.assertEqual(results[0]["note_id"], results[1]["note_id"])
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 1)
        self.assertEqual(LeadNote.objects.filter(lead=self.lead, note="Concurrent source event").count(), 1)
