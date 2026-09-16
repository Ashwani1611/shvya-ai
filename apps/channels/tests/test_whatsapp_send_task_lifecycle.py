import json
from unittest.mock import patch

from celery.exceptions import Retry
from django.db import transaction
from django.test import TransactionTestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError
from apps.channels.tasks import send_whatsapp_message_task
from apps.organizations.models import Organization


class WhatsAppSendTaskLifecycleTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.organization = Organization.objects.create(
            name="WhatsApp Send Lifecycle Org"
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Lifecycle WABA",
            phone_number_id="123456789",
            waba_id="987654321",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _message(self, *, status=WhatsAppMessage.Status.QUEUED):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.phone_number_id,
            to_number="919000000001",
            body="Hello from lifecycle test",
            status=status,
        )

    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_provider_call_runs_outside_atomic_transaction(self, send_text):
        message = self._message()

        def provider_call(*args, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            claimed = WhatsAppMessage.objects.get(pk=message.pk)
            self.assertEqual(claimed.status, "sending")
            return {"messages": [{"id": "wamid.lifecycle.success"}]}

        send_text.side_effect = provider_call

        result = send_whatsapp_message_task.run(str(message.pk))

        message.refresh_from_db()
        self.assertEqual(result["status"], "sent")
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)
        self.assertEqual(message.external_id, "wamid.lifecycle.success")

    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_permanent_meta_failure_remains_failed_after_task_returns(self, send_text):
        send_text.side_effect = WhatsAppAPIError(
            "WhatsApp API returned 400",
            status_code=400,
            response_body=json.dumps(
                {
                    "error": {
                        "code": 131009,
                        "message": "Parameter value is not valid",
                        "error_data": {
                            "details": "Recipient phone format is invalid"
                        },
                    }
                }
            ),
        )
        message = self._message()

        result = send_whatsapp_message_task.run(str(message.pk))

        message.refresh_from_db()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertTrue(message.error)
        self.assertIn("131009", message.error)

    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_transient_meta_failure_is_requeued_before_retry(self, send_text):
        send_text.side_effect = WhatsAppAPIError(
            "WhatsApp API returned 503",
            status_code=503,
            response_body=json.dumps(
                {
                    "error": {
                        "code": 2,
                        "message": "Service temporarily unavailable",
                    }
                }
            ),
        )
        message = self._message()

        with patch.object(
            send_whatsapp_message_task,
            "retry",
            side_effect=Retry(),
        ) as retry:
            with self.assertRaises(Retry):
                send_whatsapp_message_task.run(str(message.pk))

        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.QUEUED)
        self.assertEqual(message.error, "")
        retry.assert_called_once()

    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_in_flight_message_is_not_sent_by_second_worker(self, send_text):
        message = self._message(status="sending")

        result = send_whatsapp_message_task.run(str(message.pk))

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "message_not_queued")
        send_text.assert_not_called()

    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_unexpected_provider_exception_is_terminalized(self, send_text):
        send_text.side_effect = RuntimeError("unexpected provider wrapper failure")
        message = self._message()

        with self.assertRaisesRegex(RuntimeError, "unexpected provider wrapper failure"):
            send_whatsapp_message_task.run(str(message.pk))

        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertIn("unexpected provider wrapper failure", message.error)
