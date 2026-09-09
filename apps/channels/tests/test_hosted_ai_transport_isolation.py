from unittest.mock import patch

from django.test import TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.tasks import send_whatsapp_message_task
from apps.organizations.models import Organization


class HostedAITransportIsolationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Hosted AI Transport Test",
        )

    def _message_for(self, connection_type, *, ai=True):
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=connection_type,
            business_name=f"{connection_type} transport test",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number="919999999999",
            to_number="919876543210",
            body="AI reply" if ai else "Agent reply",
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload=(
                {"shvya_ai": {"origin": "engagement"}}
                if ai
                else {"shvya_hosted": {"origin": "agent"}}
            ),
        )

    @patch("services.channels.whatsapp_service.send_outbound_message")
    def test_meta_sender_leaves_hosted_ai_message_queued(self, meta_send):
        message = self._message_for(WhatsAppAccount.ConnectionType.coexisted)

        result = send_whatsapp_message_task.run(str(message.id))

        message.refresh_from_db()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "hosted_ai_transport_managed_separately")
        self.assertEqual(message.status, WhatsAppMessage.Status.QUEUED)
        meta_send.assert_not_called()

    @patch("services.channels.whatsapp_service.send_outbound_message")
    def test_non_ai_hosted_message_keeps_provider_aware_sender(self, provider_send):
        message = self._message_for(
            WhatsAppAccount.ConnectionType.coexisted,
            ai=False,
        )

        result = send_whatsapp_message_task.run(str(message.id))

        self.assertEqual(result["status"], "sent")
        provider_send.assert_called_once()
        self.assertEqual(provider_send.call_args.kwargs["message"].id, message.id)

    @patch("services.channels.whatsapp_service.send_outbound_message")
    def test_meta_sender_still_sends_api_message(self, meta_send):
        message = self._message_for(WhatsAppAccount.ConnectionType.API)

        result = send_whatsapp_message_task.run(str(message.id))

        self.assertEqual(result["status"], "sent")
        meta_send.assert_called_once()
        self.assertEqual(meta_send.call_args.kwargs["message"].id, message.id)
