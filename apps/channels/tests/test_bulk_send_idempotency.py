import json
from unittest.mock import patch

from celery.exceptions import Retry
from django.db import transaction
from django.test import TransactionTestCase

from apps.channels.models import (
    BulkMessageCampaign,
    BulkMessageRecipient,
    WhatsAppAccount,
    WhatsAppMessage,
)
from apps.channels.providers.whatsapp import WhatsAppAPIError
from apps.channels.tasks import send_bulk_recipient_task
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class BulkWhatsAppRetryIdempotencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Bulk Retry Idempotency Org"
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="Contacted",
            display_order=1,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Bulk Lead",
            phone="+919000000001",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Bulk WABA",
            phone_number_id="123456789",
            waba_id="987654321",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.campaign = BulkMessageCampaign.objects.create(
            organization=self.organization,
            account=self.account,
            name="Retry-safe campaign",
            pipeline=self.pipeline,
            body="Hello from the campaign",
            status=BulkMessageCampaign.Status.SENDING,
        )
        self.recipient = BulkMessageRecipient.objects.create(
            campaign=self.campaign,
            lead=self.lead,
        )

    @patch(
        "services.channels.bulk_service.is_within_24h_window",
        return_value=True,
    )
    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_provider_call_runs_after_claim_commit_and_uses_one_message(
        self,
        send_text,
        _within_window,
    ):
        def provider_call(*args, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.recipient.refresh_from_db()
            self.assertIsNotNone(self.recipient.message_id)
            claimed = WhatsAppMessage.objects.get(pk=self.recipient.message_id)
            self.assertEqual(claimed.status, "sending")
            return {"messages": [{"id": "wamid.bulk.success"}]}

        send_text.side_effect = provider_call

        result = send_bulk_recipient_task.run(str(self.recipient.pk))

        self.recipient.refresh_from_db()
        message = self.recipient.message
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.recipient.status, BulkMessageRecipient.Status.SENT)
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)
        self.assertEqual(message.external_id, "wamid.bulk.success")
        self.assertEqual(WhatsAppMessage.objects.filter(lead=self.lead).count(), 1)

    @patch(
        "services.channels.bulk_service.is_within_24h_window",
        return_value=True,
    )
    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_meta_5xx_retry_reuses_same_message_row(
        self,
        send_text,
        _within_window,
    ):
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

        with patch.object(
            send_bulk_recipient_task,
            "retry",
            side_effect=Retry(),
        ) as retry:
            with self.assertRaises(Retry):
                send_bulk_recipient_task.run(str(self.recipient.pk))

        self.recipient.refresh_from_db()
        first_message_id = self.recipient.message_id
        first_message = WhatsAppMessage.objects.get(pk=first_message_id)
        self.assertEqual(self.recipient.status, BulkMessageRecipient.Status.PENDING)
        self.assertEqual(first_message.status, WhatsAppMessage.Status.QUEUED)
        self.assertEqual(WhatsAppMessage.objects.filter(lead=self.lead).count(), 1)
        retry.assert_called_once()

        send_text.side_effect = None
        send_text.return_value = {"messages": [{"id": "wamid.bulk.retry-success"}]}

        result = send_bulk_recipient_task.run(str(self.recipient.pk))

        self.recipient.refresh_from_db()
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.recipient.status, BulkMessageRecipient.Status.SENT)
        self.assertEqual(self.recipient.message_id, first_message_id)
        self.assertEqual(WhatsAppMessage.objects.filter(lead=self.lead).count(), 1)
        first_message.refresh_from_db()
        self.assertEqual(first_message.status, WhatsAppMessage.Status.SENT)
        self.assertEqual(first_message.external_id, "wamid.bulk.retry-success")

    @patch(
        "services.channels.bulk_service.is_within_24h_window",
        return_value=True,
    )
    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_ambiguous_network_failure_is_not_automatically_resent(
        self,
        send_text,
        _within_window,
    ):
        send_text.side_effect = WhatsAppAPIError(
            "Network error calling WhatsApp API: read timed out",
            status_code=None,
        )

        result = send_bulk_recipient_task.run(str(self.recipient.pk))

        self.recipient.refresh_from_db()
        message = self.recipient.message
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "delivery_outcome_unknown")
        self.assertEqual(self.recipient.status, BulkMessageRecipient.Status.FAILED)
        self.assertIn("not retried automatically", self.recipient.skip_reason)
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertEqual(WhatsAppMessage.objects.filter(lead=self.lead).count(), 1)
        self.assertEqual(send_text.call_count, 1)

        second = send_bulk_recipient_task.run(str(self.recipient.pk))

        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "recipient_terminal")
        self.assertEqual(send_text.call_count, 1)
        self.assertEqual(WhatsAppMessage.objects.filter(lead=self.lead).count(), 1)

    @patch(
        "services.channels.bulk_service.is_within_24h_window",
        return_value=True,
    )
    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_existing_in_flight_message_blocks_duplicate_worker(
        self,
        send_text,
        _within_window,
    ):
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.phone_number_id,
            to_number=self.lead.phone,
            body=self.campaign.body,
            status="sending",
        )
        self.recipient.message = message
        self.recipient.save(update_fields=["message", "updated_at"])

        result = send_bulk_recipient_task.run(str(self.recipient.pk))

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "already_in_flight")
        send_text.assert_not_called()
        self.assertEqual(WhatsAppMessage.objects.filter(lead=self.lead).count(), 1)
