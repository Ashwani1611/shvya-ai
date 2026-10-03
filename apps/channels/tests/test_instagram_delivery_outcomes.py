"""Instagram transport evidence must stay in its exact source/conversation."""
from copy import deepcopy
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings

from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.instagram_delivery_outcomes import (
    instagram_file_state, record_instagram_delivery, safely_record_instagram_delivery,
)
from apps.ai_engagement.services.runtime_state import STATE_KEY
from apps.channels.instagram_models import InstagramMessage
from apps.channels.tests import test_instagram_guided_files as fixtures
from services.channels.instagram_ai import InstagramAIContextBuilder, _freeze_instagram_file_outcome
from services.channels.instagram_service import InstagramAPIError, fail_message, send_queued_message


@override_settings(OPERATIONS_PUBLIC_ORIGIN="https://testserver")
class InstagramOutcomeTests(TestCase):
    queue_file = fixtures.InstagramGuidedFileTests.queue_file

    def setUp(self):
        fixtures.InstagramGuidedFileTests.setUp(self)
        self.inbound.raw_payload = {"shvya_ai_processing": {
            "state_resolved": True, "message_id": str(self.inbound.pk),
            "resolved_file_document_id": self.document.pk, "file_share_status": "resolved_pending_send"}}
        self.inbound.save(update_fields=["raw_payload"])

    def state(self):
        self.inbound.refresh_from_db()
        return instagram_file_state(lead=self.lead, source=self.inbound)

    def test_queued_sent_and_read_are_distinct(self):
        outbound = self.queue_file()
        self.assertEqual(self.state()["status"], "queued")
        InstagramMessage.objects.filter(pk=outbound.pk).update(status="sent", external_id="accepted-mid")
        record_instagram_delivery(outbound.pk)
        self.assertEqual(self.state()["status"], "sent")
        InstagramMessage.objects.filter(pk=outbound.pk).update(status="read")
        record_instagram_delivery(outbound.pk)
        self.assertEqual(self.state()["status"], "read")
        self.assertNotEqual(self.state()["status"], "delivered")

    def test_delayed_failure_cannot_regress_a_read_receipt(self):
        outbound = self.queue_file()
        InstagramMessage.objects.filter(pk=outbound.pk).update(status="read", external_id="read-mid")
        fail_message(outbound.pk, TimeoutError("late failure"))
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, "read")
        self.assertEqual(self.state()["status"], "read")

    def test_duplicate_projection_is_idempotent(self):
        outbound = self.queue_file()
        InstagramMessage.objects.filter(pk=outbound.pk).update(status="sent", external_id="same-mid")
        record_instagram_delivery(outbound.pk)
        self.inbound.refresh_from_db(); first = deepcopy(self.inbound.raw_payload)
        record_instagram_delivery(outbound.pk)
        self.inbound.refresh_from_db()
        self.assertEqual(self.inbound.raw_payload, first)

    def test_projection_never_changes_newer_turn_or_whatsapp_history(self):
        self.lead.attributes = {STATE_KEY: {"pre_resolved_message_id": "newer-turn", "shared_files": [{"document_id": 77}]}}
        self.lead.save(update_fields=["attributes"])
        before = deepcopy(self.lead.attributes)
        outbound = self.queue_file()
        InstagramMessage.objects.filter(pk=outbound.pk).update(status="sent", external_id="old-mid")
        record_instagram_delivery(outbound.pk)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes, before)

    def test_cleared_current_selection_is_never_resurrected(self):
        outbound = self.queue_file()
        self.inbound.raw_payload["shvya_ai_processing"]["resolved_file_document_id"] = None
        self.inbound.save(update_fields=["raw_payload"])
        self.assertFalse(record_instagram_delivery(outbound.pk))
        self.assertIsNone(self.state()["document_id"])

    def test_wrong_document_version_is_not_delivery_evidence(self):
        outbound = self.queue_file()
        outbound.raw_payload["shvya_ai"]["file_version"] += 1
        outbound.status, outbound.external_id = "sent", "wrong-version"
        outbound.save(update_fields=["raw_payload", "status", "external_id"])
        self.assertFalse(record_instagram_delivery(outbound.pk))
        self.assertEqual(self.state()["status"], "resolved_pending_send")

    def test_missing_provider_identity_is_not_a_send_receipt(self):
        outbound = self.queue_file()
        InstagramMessage.objects.filter(pk=outbound.pk).update(status="sent")
        self.assertEqual(self.state()["status"], "delivery_unknown")

    def test_timeout_after_submission_is_uncertain_not_a_retry_permission(self):
        outbound = self.queue_file()
        outbound.raw_payload["shvya_send_started_at"] = "test-started"
        outbound.save(update_fields=["raw_payload"])
        fail_message(outbound.pk, InstagramAPIError("timeout", transient=True))
        self.assertEqual(self.state()["status"], "delivery_unknown")
        decision = EngagementDecision(should_engage=True, message="I couldn't confirm the file.", file_document_id=self.document.pk,
                                      crm_actions=[], reason="NORMAL_CONVERSATION", model="test")
        result = _freeze_instagram_file_outcome(decision=decision, lead=self.lead, source=self.inbound)
        self.assertIsNone(result.file_document_id)

    def test_projection_outage_does_not_repeat_an_accepted_send(self):
        outbound = self.queue_file()
        with patch("services.channels.instagram_service._graph_post", return_value={"message_id": "provider-mid"}) as provider, \
             patch("apps.ai_engagement.services.instagram_delivery_outcomes.record_instagram_delivery", side_effect=DatabaseError("offline")):
            with self.captureOnCommitCallbacks(execute=True):
                result = send_queued_message(outbound)
            self.assertEqual(result.status, "sent")
            provider.assert_called_once()
            self.assertFalse(safely_record_instagram_delivery(outbound.pk))

    def test_instagram_context_names_exact_conversation(self):
        context = InstagramAIContextBuilder(conversation_id=self.conversation.pk).build(organization=self.org, lead=self.lead)
        self.assertEqual(context.conversation["conversation_id"], str(self.conversation.pk))

    def test_validation_failure_does_not_reauthorize_file_after_final_pass(self):
        decision = EngagementDecision(should_engage=True, message="Unable to verify.", file_document_id=None,
                                      crm_actions=[], reason="UNKNOWN_INFORMATION", model="test", final_validation_failed=True)
        self.assertIsNone(_freeze_instagram_file_outcome(decision=decision, lead=self.lead, source=self.inbound).file_document_id)
