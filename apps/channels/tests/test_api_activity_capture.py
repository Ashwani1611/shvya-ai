from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.test import SimpleTestCase
import requests

from apps.channels.activity import observe_message_send
from apps.channels.activity_signals import whatsapp_message_state, instagram_message_state


class ChannelActivityCaptureTests(SimpleTestCase):
    def message(self, *, status="sent", connection_type="api"):
        return SimpleNamespace(pk=uuid.uuid4(), organization_id=uuid.uuid4(), account_id=uuid.uuid4(),
                               account=SimpleNamespace(connection_type=connection_type, is_operations_test=False),
                               external_id="wamid.provider-reference", direction="outbound", status=status,
                               message_type="text", lead_id=uuid.uuid4(), body="private customer content",
                               error="private provider diagnostic", raw_payload={"errors": [{"code": 131049,
                               "message": "Bearer private-token buyer@example.com"}]})

    @patch("apps.channels.activity_signals.record_activity")
    def test_failed_receipt_records_provider_code_without_body_or_error_payload(self, record):
        message = self.message(status="failed")
        whatsapp_message_state(None, message, False, update_fields={"status", "raw_payload"})
        fields = record.call_args.kwargs
        self.assertEqual(fields["error_code"], "131049")
        self.assertEqual(fields["status"], "failed")
        self.assertEqual(fields["entity_id"], str(message.pk))
        self.assertNotIn("private", repr(fields))
        self.assertNotIn("buyer@example.com", repr(fields))

    @patch("apps.channels.activity_signals.record_activity")
    def test_accepted_and_delivered_are_distinct_and_read_flag_writes_ignored(self, record):
        message = self.message()
        whatsapp_message_state(None, message, False, update_fields={"status"})
        self.assertEqual(record.call_args.kwargs["status"], "accepted")
        message.status = "delivered"
        whatsapp_message_state(None, message, False, update_fields={"status"})
        self.assertEqual(record.call_args.kwargs["status"], "completed")
        record.reset_mock()
        whatsapp_message_state(None, message, False, update_fields={"is_read"})
        record.assert_not_called()

    @patch("apps.channels.activity_signals.record_activity")
    def test_hosted_and_instagram_are_separate_integrations(self, record):
        message = self.message(connection_type="hosted")
        whatsapp_message_state(None, message, True)
        self.assertEqual(record.call_args.kwargs["integration"], "whatsapp_hosted")
        instagram_message_state(None, message, True)
        self.assertEqual(record.call_args.kwargs["integration"], "instagram")

    @patch("apps.channels.activity.finish_activity")
    @patch("apps.channels.activity.start_activity", return_value=None)
    def test_refreshed_instagram_return_value_is_authoritative_for_observation(self, start, finish):
        original = self.message(status="queued")
        refreshed = self.message(status="sent")
        refreshed.external_id = "confirmed-provider-id"

        @observe_message_send("instagram")
        def send(message):
            return refreshed

        self.assertIs(send(original), refreshed)
        self.assertEqual(finish.call_args.kwargs["status"], "accepted")
        self.assertEqual(finish.call_args.kwargs["provider_reference"], "confirmed-provider-id")

    @patch("apps.channels.activity.finish_activity")
    @patch("apps.channels.activity.start_activity", return_value=None)
    def test_wrapped_provider_timeout_stays_unknown_and_propagates(self, start, finish):
        @observe_message_send("whatsapp_api")
        def send(message):
            try:
                raise requests.Timeout("credential-containing transport detail")
            except requests.Timeout as exc:
                raise RuntimeError("provider failure") from exc

        with self.assertRaises(RuntimeError):
            send(self.message())
        self.assertEqual(finish.call_args.kwargs["status"], "unknown")
        self.assertNotIn("credential", repr(finish.call_args))
