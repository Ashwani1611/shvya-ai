"""Database regressions for source-bound outcomes; no external sends."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, SimpleTestCase

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.canonical_architecture import (
    ResponseActionValidator, StateReconciler, _record_ai_file_delivery, _reconciled_for_context,
)
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.file_delivery_receipts import (
    FILE_ID_KEY, FILE_STATUS_KEY, SOURCE_KEY, record_file_delivery, source_file_state,
)
from apps.ai_engagement.services.runtime_state import STATE_KEY
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class FileReceiptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="File receipt test")
        cls.other = Organization.objects.create(name="Other receipt tenant")
        cls.pipeline = Pipeline.objects.create(organization=cls.org, name="Sales", country_code="+91", phone_number="9000000881")
        cls.lead = Lead.objects.create(organization=cls.org, pipeline=cls.pipeline,
            stage=cls.pipeline.stages.get(name="New leads"), phone="+919000000882", name="Test lead")
        cls.account = WhatsAppAccount.objects.create(organization=cls.org, phone_number_id="receipt-test-a")
        cls.foreign_account = WhatsAppAccount.objects.create(organization=cls.other, phone_number_id="receipt-test-b")
        cls.document = Document.objects.create(organization=cls.org, name="Test brochure")
        cls.other_document = Document.objects.create(organization=cls.other, name="Private document")

    def setUp(self):
        self.inbound = WhatsAppMessage.objects.create(organization=self.org, account=self.account,
            lead=self.lead, direction="inbound", status="received", from_number="9000000882", to_number="9000000881")
        self.outbound = self.make_outbound()
        self.set_processing({"resolved_file_document_id": self.document.pk, "file_share_status": "resolved_pending_send"})
        self.lead.attributes = {STATE_KEY: {SOURCE_KEY: str(self.inbound.pk), FILE_ID_KEY: self.document.pk,
            FILE_STATUS_KEY: "resolved_pending_send", "other_state": "preserved"}}
        self.lead.save(update_fields=["attributes"])

    def make_outbound(self, **kwargs):
        data = {"organization": self.org, "account": self.account, "lead": self.lead,
            "direction": "outbound", "status": "sent", "from_number": "9000000881", "to_number": "9000000882",
            "message_type": "document", "media_payload": {"source": "document", "document_id": self.document.pk},
            "raw_payload": {"shvya_ai": {"source_inbound_message_id": str(self.inbound.pk)}}}
        data.update(kwargs)
        return WhatsAppMessage.objects.create(**data)

    def set_processing(self, processing):
        self.inbound.raw_payload = {"shvya_ai_processing": processing}
        self.inbound.save(update_fields=["raw_payload"])

    def processing(self):
        self.inbound.refresh_from_db()
        return self.inbound.raw_payload["shvya_ai_processing"]

    def runtime(self):
        self.lead.refresh_from_db()
        return self.lead.attributes[STATE_KEY]

    def project(self):
        return source_file_state(lead=self.lead, source=self.inbound,
            processing=self.processing(), runtime=self.runtime())

    def test_success_records_actual_outbound_without_sending(self):
        before = WhatsAppMessage.objects.count()
        self.assertTrue(record_file_delivery(message_id=self.outbound.pk, status="sent"))
        self.assertEqual(self.processing()["file_share_status"], "sent")
        self.assertEqual(self.runtime()["other_state"], "preserved")
        self.assertEqual(WhatsAppMessage.objects.count(), before)

    def test_duplicate_receipt_is_idempotent(self):
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        old = deepcopy(self.processing())
        old_runtime = deepcopy(self.runtime())
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        self.assertEqual(self.processing(), old)
        self.assertEqual(self.runtime(), old_runtime)

    def test_delayed_receipt_does_not_overwrite_new_turn_selection(self):
        newer = self.make_outbound(direction="inbound", status="received")
        runtime = {SOURCE_KEY: str(newer.pk), FILE_ID_KEY: 999, FILE_STATUS_KEY: "resolved_pending_send", "other_state": "preserved"}
        self.lead.attributes = {STATE_KEY: runtime}
        self.lead.save(update_fields=["attributes"])
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        updated = self.runtime()
        self.assertEqual((updated[SOURCE_KEY], updated[FILE_ID_KEY], updated[FILE_STATUS_KEY]),
                         (str(newer.pk), 999, "resolved_pending_send"))
        self.assertEqual(self.processing()["file_share_status"], "sent")

    def test_wrapper_hint_cannot_invent_delivery(self):
        record_file_delivery(message_id=self.outbound.pk, status="read")
        self.assertEqual(self.processing()["file_share_status"], "sent")

    def test_queued_message_never_becomes_sent_from_wrapper_hint(self):
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="queued")
        self.assertFalse(record_file_delivery(message_id=self.outbound.pk, status="sent"))
        self.assertEqual(self.processing()["file_share_status"], "resolved_pending_send")

    def test_persisted_read_survives_stale_send_result(self):
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="read")
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        self.assertEqual(self.processing()["file_share_status"], "read")

    def test_recorded_delivery_does_not_regress_after_transport_regression(self):
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="delivered")
        record_file_delivery(message_id=self.outbound.pk, status="delivered")
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="sent")
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        self.assertEqual(self.processing()["file_share_status"], "delivered")

    def test_failure_after_acceptance_removes_that_attempt_from_shared_history(self):
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="failed")
        _record_ai_file_delivery(message_id=self.outbound.pk, status="failed", reason="private provider text")
        self.assertEqual(self.processing()["file_share_reason"], "file_delivery_failed")
        self.assertEqual(self.runtime()["shared_files"], [])
        self.assertNotIn("private", str(self.processing()))

    def test_new_successful_retry_can_replace_failed_older_attempt(self):
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="failed")
        record_file_delivery(message_id=self.outbound.pk, status="failed")
        retry = self.make_outbound()
        self.assertTrue(record_file_delivery(message_id=retry.pk, status="sent"))
        self.assertEqual(self.processing()["file_share_message_id"], str(retry.pk))
        self.assertNotIn("file_share_reason", self.processing())
        self.assertFalse(record_file_delivery(message_id=self.outbound.pk, status="failed"))
        self.assertEqual(self.processing()["file_share_status"], "sent")

    def test_second_attempt_cannot_overwrite_successful_first_attempt(self):
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        other = self.make_outbound()
        self.assertFalse(record_file_delivery(message_id=other.pk, status="sent"))
        self.assertEqual(self.processing()["file_share_message_id"], str(self.outbound.pk))

    def test_foreign_document_is_rejected(self):
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(media_payload={"source": "document", "document_id": self.other_document.pk})
        self.assertFalse(record_file_delivery(message_id=self.outbound.pk, status="sent"))

    def test_foreign_account_is_rejected(self):
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(account=self.foreign_account)
        self.assertFalse(record_file_delivery(message_id=self.outbound.pk, status="sent"))

    def test_foreign_source_is_rejected(self):
        foreign = WhatsAppMessage.objects.create(organization=self.other, account=self.foreign_account,
            direction="inbound", status="received", from_number="a", to_number="b")
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(raw_payload={"shvya_ai": {"source_inbound_message_id": str(foreign.pk)}})
        self.assertFalse(record_file_delivery(message_id=self.outbound.pk, status="sent"))

    def test_bool_document_id_and_malformed_source_are_rejected(self):
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(media_payload={"source": "document", "document_id": True})
        self.assertFalse(record_file_delivery(message_id=self.outbound.pk, status="sent"))
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(raw_payload={"shvya_ai": {"source_inbound_message_id": "bad"}})
        self.assertFalse(record_file_delivery(message_id=self.outbound.pk, status="sent"))

    def test_cleared_selection_cannot_be_resurrected(self):
        self.set_processing({"resolved_file_document_id": None})
        self.assertFalse(record_file_delivery(message_id=self.outbound.pk, status="sent"))

    def test_corrupt_old_history_does_not_break_success(self):
        state = self.runtime()
        state["shared_files"] = [None, {"document_id": "bad"}]
        self.lead.attributes = {STATE_KEY: state}
        self.lead.save(update_fields=["attributes"])
        self.assertTrue(record_file_delivery(message_id=self.outbound.pk, status="sent"))
        self.assertEqual(len(self.runtime()["shared_files"]), 1)

    def test_projection_reads_latest_delivery_without_another_send(self):
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="delivered")
        self.assertEqual(self.project()["status"], "delivered")

    def test_projection_preserves_queued_distinction(self):
        self.set_processing({"resolved_file_document_id": self.document.pk,
            "file_share_status": "resolved_pending_send", "file_share_message_id": str(self.outbound.pk)})
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="queued")
        self.assertEqual(self.project()["status"], "queued")

    def test_unbound_sent_claim_becomes_uncertain(self):
        self.set_processing({"resolved_file_document_id": self.document.pk, "file_share_status": "sent"})
        self.assertEqual(self.project()["status"], "delivery_unknown")

    def test_projection_rejects_foreign_message_reference(self):
        foreign = self.make_outbound(organization=self.other, account=self.foreign_account)
        self.set_processing({"resolved_file_document_id": self.document.pk,
            "file_share_status": "sent", "file_share_message_id": str(foreign.pk)})
        self.assertEqual(self.project()["status"], "delivery_unknown")

    def test_reconciled_snapshot_file_outcome_updates(self):
        self.set_processing({"resolved_file_document_id": self.document.pk,
            "reconciled_state": {"source_message_id": str(self.inbound.pk),
                "file_share": {"document_id": self.document.pk, "status": "resolved_pending_send"}}})
        record_file_delivery(message_id=self.outbound.pk, status="sent")
        self.assertEqual(self.processing()["reconciled_state"]["file_share"]["status"], "sent")
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="read")
        context = SimpleNamespace(conversation={"messages": [{"id": str(self.inbound.pk), "direction": "inbound"}]})
        self.assertEqual(_reconciled_for_context(lead=self.lead, context=context)["file_share"]["status"], "read")

    def test_new_turn_does_not_inherit_previous_actions_or_file(self):
        state = self.runtime()
        state["pre_resolved_actions"] = ["create_reminder"]
        self.lead.attributes = {STATE_KEY: state}
        self.lead.save(update_fields=["attributes"])
        new_source = self.make_outbound(direction="inbound", status="received", raw_payload={})
        with patch("apps.ai_engagement.services.transactional_turn_runtime._requirements_for_turn", return_value=[]):
            snapshot = StateReconciler().build(lead=self.lead, source_message_id=new_source.pk)
        self.assertEqual(snapshot["workflow"]["action_types"], [])
        self.assertIsNone(snapshot["file_share"]["document_id"])

    def test_explicit_empty_actions_do_not_inherit_runtime_actions(self):
        state = self.runtime()
        state["pre_resolved_actions"] = ["create_reminder"]
        self.lead.attributes = {STATE_KEY: state}
        self.lead.save(update_fields=["attributes"])
        self.set_processing({"pre_resolved_actions": [], "resolved_file_document_id": None})
        with patch("apps.ai_engagement.services.transactional_turn_runtime._requirements_for_turn", return_value=[]):
            snapshot = StateReconciler().build(lead=self.lead, source_message_id=self.inbound.pk)
        self.assertEqual(snapshot["workflow"]["action_types"], [])

    def test_mismatched_reconciled_source_is_not_used(self):
        self.set_processing({"reconciled_state": {"source_message_id": str(self.outbound.pk)}})
        context = SimpleNamespace(conversation={"messages": [{"id": str(self.inbound.pk), "direction": "inbound"}]})
        self.assertIsNone(_reconciled_for_context(lead=self.lead, context=context))


class FinalReceiptValidationTests(SimpleTestCase):
    def decision(self):
        return EngagementDecision(should_engage=True, message="The plan is available. I've shared the brochure with you.",
            file_document_id=12, crm_actions=[], reason="NORMAL_CONVERSATION", model="test")

    def test_failed_file_does_not_claim_it_is_being_sent(self):
        result = ResponseActionValidator().validate(decision=self.decision(),
            reconciled_state={"file_share": {"document_id": 12, "status": "failed"}})
        self.assertIsNone(result.file_document_id)
        self.assertNotIn("I'm sending", result.message)
        self.assertNotIn("I've shared", result.message)
        self.assertIn("The plan is available.", result.message)

    def test_uncertain_file_is_not_claimed_failed_or_resent(self):
        result = ResponseActionValidator().validate(decision=self.decision(),
            reconciled_state={"file_share": {"document_id": 12, "status": "delivery_unknown"}})
        self.assertIsNone(result.file_document_id)
        self.assertIn("couldn't confirm", result.message)

    def test_projection_failure_does_not_repeat_provider_operation(self):
        with patch("apps.ai_engagement.services.file_delivery_receipts.record_file_delivery", side_effect=DatabaseError("private")):
            _record_ai_file_delivery(message_id="unused", status="sent")
