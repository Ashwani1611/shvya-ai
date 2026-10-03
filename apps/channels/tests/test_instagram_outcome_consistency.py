"""Instagram-specific transport outcomes and late callback regressions."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.instagram_outcomes import file_outcome
from apps.ai_engagement.services.transactional_decision_reuse import operational_state_for_context
from apps.ai_engagement.tests import test_instagram_evidence_context as fixtures
from apps.channels.instagram_models import InstagramConversation, InstagramMessage
from services.channels.instagram_ai import InstagramAIContextBuilder
from services.channels.instagram_service import send_queued_message, fail_message, requeue_explicitly_rejected_message


class InstagramOutcomeConsistencyTests(TestCase):
    def setUp(self):
        fixtures.InstagramEvidenceContextTests.setUp(self)
        self.document = Document.objects.create(organization=self.org, name="Guide", file="guide.pdf",
            share_instruction="Send when requested.", processing_status="completed")
        self.source.raw_payload = {"shvya_ai_processing": {"resolved_file_document_id": self.document.pk}}
        self.source.save(update_fields=["raw_payload"])

    def outbound(self, *, status="queued", external_id=None, conversation=None, claimed=False, file=True):
        raw = {"shvya_ai": {"source_inbound_message_id": str(self.source.pk)}}
        if file:
            raw["shvya_ai"]["file_document_id"] = self.document.pk
        if claimed:
            raw["shvya_send_claimed_at"] = timezone.now().isoformat()
        return InstagramMessage.objects.create(organization=self.org, account=self.account,
            conversation=conversation or self.conversation, direction="outbound", status=status,
            external_id=external_id, sender_id=self.account.ig_user_id, recipient_id=self.conversation.participant_id,
            body="Here are the details.", raw_payload=raw)

    def test_queued_file_is_not_confirmed_as_sent(self):
        self.outbound()
        outcome = file_outcome(source=self.source, lead=self.lead)
        self.assertEqual(outcome["status"], "queued")
        self.assertFalse(outcome["provider_accepted"])
        self.assertFalse(outcome["recipient_read_confirmed"])
        self.assertFalse(outcome["delivery_confirmation_available"])

    def test_claimed_unacknowledged_send_is_uncertain(self):
        self.outbound(claimed=True)
        self.assertEqual(file_outcome(source=self.source, lead=self.lead)["status"], "outcome_unknown")

    def test_generation_reads_provider_acceptance_and_read_separately(self):
        row = self.outbound(status="sent", external_id="accepted")
        builder = InstagramAIContextBuilder(conversation_id=self.conversation.pk)
        context = builder.build(organization=self.org, lead=self.lead)
        first = operational_state_for_context(context)["resolved_actions"]["file_share"]
        self.assertTrue(first["provider_accepted"])
        self.assertFalse(first["recipient_read_confirmed"])
        row.status = "read"
        row.save(update_fields=["status"])
        second = operational_state_for_context(context)["resolved_actions"]["file_share"]
        self.assertTrue(second["recipient_read_confirmed"])
        self.assertFalse(second["delivery_confirmation_available"])

    def test_other_conversation_file_does_not_contaminate_this_turn(self):
        other = InstagramConversation.objects.create(organization=self.org, account=self.account,
            lead=self.lead, participant_id="another-thread")
        self.outbound(status="sent", external_id="elsewhere", conversation=other)
        self.assertEqual(file_outcome(source=self.source, lead=self.lead)["status"], "resolved_pending_send")

    def test_older_source_receipt_cannot_overwrite_new_file_choice(self):
        old = self.outbound(status="sent", external_id="old-file")
        new_doc = Document.objects.create(organization=self.org, name="New guide")
        new = InstagramMessage.objects.create(organization=self.org, account=self.account,
            conversation=self.conversation, direction="inbound", status="received", external_id="new-question",
            sender_id=self.source.sender_id, recipient_id=self.source.recipient_id, body="Another guide please",
            raw_payload={"shvya_ai_processing": {"resolved_file_document_id": new_doc.pk}})
        old.status = "read"
        old.save(update_fields=["status"])
        outcome = file_outcome(source=new, lead=self.lead)
        self.assertEqual(outcome["document_id"], new_doc.pk)
        self.assertEqual(outcome["status"], "resolved_pending_send")

    def test_cleared_file_selection_is_not_resurrected(self):
        self.outbound(status="sent", external_id="old-file")
        self.source.raw_payload["shvya_ai_processing"]["resolved_file_document_id"] = None
        self.assertIsNone(file_outcome(source=self.source, lead=self.lead))

    def test_late_failure_does_not_regress_sent_or_read(self):
        for status in ("sent", "read"):
            row = self.outbound(status=status, external_id=status)
            fail_message(row.pk, RuntimeError("late failure"))
            row.refresh_from_db()
            self.assertEqual(row.status, status)
            self.assertEqual(row.error, "")

    def test_unaccepted_failed_send_retains_claim_for_reconciliation(self):
        row = self.outbound(claimed=True)
        fail_message(row.pk, RuntimeError("network interrupted"))
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        self.assertIn("shvya_send_claimed_at", row.raw_payload)
        self.assertEqual(file_outcome(source=self.source, lead=self.lead)["status"], "outcome_unknown")

    def test_read_row_without_provider_id_cannot_be_requeued(self):
        row = self.outbound(status="read")
        self.assertFalse(requeue_explicitly_rejected_message(row.pk))
        row.refresh_from_db()
        self.assertEqual(row.status, "read")

    def test_acknowledged_row_cannot_be_resent_even_if_failure_status_is_stale(self):
        row = self.outbound(status="failed", external_id="accepted")
        with patch("services.channels.instagram_service._graph_post") as send:
            result = send_queued_message(row)
        send.assert_not_called()
        self.assertEqual(result.external_id, "accepted")

    def test_send_refreshes_read_state_and_metadata_after_network_io(self):
        row = self.outbound(file=False)
        sent_at = timezone.now() - timedelta(seconds=2)
        def network(*args, **kwargs):
            payload = {**row.raw_payload, "audit_marker": "concurrent"}
            InstagramMessage.objects.filter(pk=row.pk).update(status="read", is_read=True,
                external_id="mid-race", sent_at=sent_at, raw_payload=payload)
            return {"message_id": "mid-race"}
        with patch("services.channels.instagram_service._graph_post", side_effect=network) as send:
            result = send_queued_message(row)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result.status, "read")
        self.assertEqual(result.sent_at, sent_at)
        self.assertEqual(result.raw_payload["audit_marker"], "concurrent")

    def test_echo_read_receipt_is_merged_into_source_bound_row(self):
        row = self.outbound(file=False)
        sent_at = timezone.now() - timedelta(seconds=2)
        def network(*args, **kwargs):
            echo = self.outbound(status="read", external_id="mid-echo", file=False)
            echo.raw_payload, echo.sent_at = {}, sent_at
            echo.save(update_fields=["raw_payload", "sent_at"])
            return {"message_id": "mid-echo"}
        with patch("services.channels.instagram_service._graph_post", side_effect=network):
            result = send_queued_message(row)
        self.assertEqual(result.pk, row.pk)
        self.assertEqual(result.status, "read")
        self.assertEqual(result.sent_at, sent_at)
        self.assertEqual(result.raw_payload["shvya_ai"]["source_inbound_message_id"], str(self.source.pk))
        self.assertEqual(InstagramMessage.objects.filter(external_id="mid-echo").count(), 1)

    def test_forged_context_thread_cannot_load_the_source_outcome(self):
        self.outbound(status="sent", external_id="real-file")
        context = InstagramAIContextBuilder(conversation_id=self.conversation.pk).build(
            organization=self.org, lead=self.lead)
        context.conversation["id"] = "invalid-thread"
        resolved = operational_state_for_context(context)["resolved_actions"]
        self.assertEqual(resolved["authority"], "source_unavailable")
        self.assertNotIn("file_share", resolved)
