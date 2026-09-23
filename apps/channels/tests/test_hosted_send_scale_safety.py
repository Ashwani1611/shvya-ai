from unittest.mock import Mock, patch

from celery.exceptions import Retry
from django.core.cache import cache
from django.db import transaction
from django.test import TransactionTestCase, override_settings

from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp_web import WhatsAppWebGatewayError
from apps.organizations.models import Organization


class HostedSendTaskScaleSafetyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Hosted Send Scale Org",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Scale",
            display_phone_number="+919000000301",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _message(self, status=WhatsAppMessage.Status.QUEUED):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.display_phone_number,
            to_number="+919000000302",
            body="Hosted scale safety",
            status=status,
        )

    @patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh")
    @patch("apps.channels.hosted_send_tasks.finalize_hosted_send")
    @patch("apps.channels.hosted_gateway_routing.gateway_client_for_account")
    def test_provider_call_runs_after_atomic_claim(
        self,
        gateway_client,
        finalize_send,
        queue_refresh,
    ):
        message = self._message()
        client = Mock()

        def send_message(**kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            claimed = WhatsAppMessage.objects.get(pk=message.pk)
            self.assertEqual(claimed.status, "sending")
            return {"messageId": "hosted-provider-1"}

        client.send_message.side_effect = send_message
        gateway_client.return_value = client

        result = send_hosted_whatsapp_message_task.run(str(message.pk))

        message.refresh_from_db()
        self.assertEqual(result["status"], "sent")
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)
        self.assertEqual(message.external_id, "wweb:hosted-provider-1")
        finalize_send.assert_called_once()
        queue_refresh.assert_called_once()

    @override_settings(
        HOSTED_WHATSAPP_ACCOUNT_SENDS_PER_MINUTE=1,
        HOSTED_WHATSAPP_GLOBAL_SENDS_PER_MINUTE=10,
    )
    @patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh")
    @patch("apps.channels.hosted_send_tasks.finalize_hosted_send")
    @patch("apps.channels.hosted_gateway_routing.gateway_client_for_account")
    def test_hosted_account_admission_defers_second_message(
        self,
        gateway_client,
        finalize_send,
        queue_refresh,
    ):
        cache.clear()
        client = Mock()
        client.send_message.return_value = {"messageId": "hosted-first"}
        gateway_client.return_value = client
        first = self._message()
        second = self._message()

        first_result = send_hosted_whatsapp_message_task.run(str(first.pk))
        with patch.object(send_hosted_whatsapp_message_task, "apply_async") as republish:
            second_result = send_hosted_whatsapp_message_task.run(str(second.pk))

        second.refresh_from_db()
        self.assertEqual(first_result["status"], "sent")
        self.assertEqual(second_result["status"], "deferred")
        self.assertEqual(second.status, WhatsAppMessage.Status.QUEUED)
        self.assertEqual(client.send_message.call_count, 1)
        republish.assert_called_once()
        finalize_send.assert_called_once()
        queue_refresh.assert_called_once()

    @patch("apps.channels.hosted_gateway_routing.gateway_client_for_account")
    def test_in_flight_message_is_not_sent_by_second_worker(self, gateway_client):
        message = self._message(status="sending")

        result = send_hosted_whatsapp_message_task.run(str(message.pk))

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "message_not_queued")
        gateway_client.assert_not_called()

    @patch("apps.channels.hosted_gateway_routing.gateway_client_for_account")
    def test_gateway_502_is_uncertain_and_not_replayed(self, gateway_client):
        message = self._message()
        client = Mock()
        client.send_message.side_effect = WhatsAppWebGatewayError(
            "Gateway send failed after entering sendMessage",
            status_code=502,
        )
        gateway_client.return_value = client

        with patch.object(send_hosted_whatsapp_message_task, "retry") as retry:
            result = send_hosted_whatsapp_message_task.run(str(message.pk))

        message.refresh_from_db()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "provider_outcome_uncertain")
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        retry.assert_not_called()

    @patch("apps.channels.hosted_gateway_routing.gateway_client_for_account")
    def test_session_not_running_requeues_with_bounded_jitter(self, gateway_client):
        message = self._message()
        client = Mock()
        client.send_message.side_effect = WhatsAppWebGatewayError(
            "Session is not running",
            status_code=409,
        )
        gateway_client.return_value = client

        with patch.object(
            send_hosted_whatsapp_message_task,
            "retry",
            side_effect=Retry(),
        ) as retry:
            with self.assertRaises(Retry):
                send_hosted_whatsapp_message_task.run(str(message.pk))

        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.QUEUED)
        countdown = retry.call_args.kwargs["countdown"]
        self.assertGreaterEqual(countdown, 10)
        self.assertLessEqual(countdown, 16)
