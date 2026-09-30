from unittest.mock import patch

from django.test import TestCase

from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp_web import WhatsAppWebGatewayError
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization
from services.channels.hosted_automation_service import HostedAutomationPaused, set_health_enabled
from services.channels.hosted_whatsapp_transport import send_hosted_message


class HostedAIReliabilityRegressionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Hosted AI Reliability",
            settings={"hosted_account_enabled": True},
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted AI Reliability",
            phone_number_id="+918700274739",
            display_phone_number="+918700274739",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization, name="Hosted AI", country_code="+91",
            phone_number="8700274739", ai_enabled=True,
        )
        self.lead = Lead.objects.create(
            organization=self.organization, pipeline=self.pipeline,
            stage=self.pipeline.stages.get(name="New leads"),
            name="AI Prospect", phone="+919999999999", ai_enabled=True,
        )
        # Persist the authenticated conversation without firing queue signals;
        # these tests isolate final delivery and health, not inbound enqueue.
        WhatsAppMessage.objects.bulk_create([WhatsAppMessage(
            organization=self.organization, account=self.account, lead=self.lead,
            direction="inbound", status="received", body="Hello",
            from_number=self.lead.phone, to_number=self.account.display_phone_number,
        )])

    def _create_sent_messages(self, count):
        WhatsAppMessage.objects.bulk_create(
            [
                WhatsAppMessage(
                    organization=self.organization,
                    account=self.account,
                    direction=WhatsAppMessage.Direction.OUTBOUND,
                    external_id=f"wweb:health-off-{index}",
                    from_number=self.account.display_phone_number,
                    to_number=f"+9198765{index:05d}",
                    body="Already sent",
                    message_type=WhatsAppMessage.MessageType.TEXT,
                    status=WhatsAppMessage.Status.SENT,
                    raw_payload={"gateway_send": {"messageId": f"sent-{index}"}},
                )
                for index in range(count)
            ]
        )

    def _queued_automation(self, *, payload=None, body="AI reply"):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.display_phone_number,
            to_number="+919999999999",
            body=body,
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload=payload or {"shvya_ai": {"origin": "engagement"}},
        )

    @patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh")
    @patch(
        "apps.channels.providers.whatsapp_web.WhatsAppWebClient.send_message",
        return_value={"messageId": "AI-AFTER-250-HEALTH-OFF"},
    )
    def test_account_health_off_allows_automation_beyond_250_messages(
        self,
        provider_send,
        _refresh,
    ):
        self._create_sent_messages(250)
        snapshot = set_health_enabled(account=self.account, enabled=False)
        self.assertFalse(snapshot["enabled"])
        self.assertFalse(snapshot["paused"])

        message = self._queued_automation(
            payload={"shvya_ai": {"origin": "engagement"}},
            body="Message 251",
        )

        result = send_hosted_whatsapp_message_task.run(str(message.id))

        self.assertEqual(result["status"], "sent")
        provider_send.assert_called_once()
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)

    @patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh")
    @patch(
        "services.channels.hosted_health_guard.reserve_hosted_automation_send",
        return_value={"reserved": False, "blocked_until": None},
    )
    @patch(
        "apps.channels.providers.whatsapp_web.WhatsAppWebClient.send_message",
        side_effect=WhatsAppWebGatewayError("gateway temporarily unavailable", status_code=503),
    )
    def test_transient_gateway_failure_keeps_generated_automation_queued_for_retry(
        self,
        _provider_send,
        _reserve,
        _refresh,
    ):
        message = self._queued_automation(
            payload={"shvya_welcome": {"trigger": "lead_created"}},
        )

        with self.assertRaises(HostedAutomationPaused) as raised:
            send_hosted_message(message=message, defer_on_pause=True)

        self.assertIsNotNone(raised.exception.paused_until)
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.QUEUED)
        self.assertIn("retry scheduled", message.error)
