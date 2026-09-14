from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_health_guard import (
    message_is_hosted_automation,
    sync_hosted_health_from_messages,
)
from services.channels.hosted_whatsapp_service import create_hosted_account


class HostedAccountHealthHardCapTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted Health Hard Cap Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-health-hard-cap@example.com",
            password="test-password",
            name="Hosted Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Hosted Health Sales",
            country_code="+91",
            phone_number="8700274739",
            owner=self.user,
            ai_enabled=True,
        )
        Stage.objects.get_or_create(
            pipeline=self.pipeline,
            display_order=1,
            defaults={"name": "New"},
        )
        self.account, _pipeline, _created = create_hosted_account(
            organization=self.org,
            created_by=self.user,
            country_code="+91",
            phone_number="8700274739",
        )
        self.account.status = self.account.Status.CONNECTED
        self.account.save(update_fields=["status", "updated_at"])

    def _create_sent_messages(self, count):
        WhatsAppMessage.objects.bulk_create(
            [
                WhatsAppMessage(
                    organization=self.org,
                    account=self.account,
                    direction=WhatsAppMessage.Direction.OUTBOUND,
                    external_id=f"wweb:health-hard-cap-{index}",
                    from_number="+918700274739",
                    to_number=f"+9198765{index:05d}",
                    body="Already sent",
                    message_type=WhatsAppMessage.MessageType.TEXT,
                    status=WhatsAppMessage.Status.SENT,
                    raw_payload={"fromMe": True, "isHistory": False},
                )
                for index in range(count)
            ]
        )

    def _queued_message(self, *, raw_payload, body="Queued message"):
        return WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number="+918700274739",
            to_number="+919999999999",
            body=body,
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload=raw_payload,
        )

    def test_all_shvya_automation_types_are_health_gated(self):
        for payload in (
            {"shvya_ai": {"origin": "engagement"}},
            {"shvya_auto_followup": {"step": 1}},
            {"shvya_welcome": {"trigger": "lead_created"}},
        ):
            message = self._queued_message(raw_payload=payload)
            self.assertTrue(message_is_hosted_automation(message))

        manual = self._queued_message(
            raw_payload={"shvya_hosted": {"origin": "agent"}},
            body="Human reply",
        )
        self.assertFalse(message_is_hosted_automation(manual))

    def test_reconciliation_repairs_inflated_sent_count(self):
        self._create_sent_messages(3)
        health = sync_hosted_health_from_messages(account=self.account)
        self.assertEqual(health.total_messages_sent, 3)

        # Reproduce the stale/inflated counter left by the older counting path.
        health.total_messages_sent = 252
        health.save(update_fields=["total_messages_sent", "updated_at"])

        repaired = sync_hosted_health_from_messages(account=self.account)
        self.assertEqual(repaired.total_messages_sent, 3)
        self.assertEqual(repaired.window_messages_sent, 3)

    @patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh")
    @patch("apps.channels.hosted_send_tasks.WhatsAppWebClient.send_message")
    def test_welcome_is_not_sent_after_250_limit(self, provider_send, _refresh):
        self._create_sent_messages(250)
        health = sync_hosted_health_from_messages(account=self.account)
        self.assertEqual(health.window_messages_sent, 250)
        self.assertIsNotNone(health.paused_until)

        welcome = self._queued_message(
            raw_payload={
                "shvya_welcome": {
                    "trigger": "lead_created",
                    "source": "organization_information_ai",
                }
            },
            body="AI welcome",
        )

        with patch.object(send_hosted_whatsapp_message_task, "apply_async") as reschedule:
            result = send_hosted_whatsapp_message_task.run(str(welcome.id))

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "account_health_pause")
        provider_send.assert_not_called()
        reschedule.assert_called_once()
        welcome.refresh_from_db()
        self.assertEqual(welcome.status, WhatsAppMessage.Status.QUEUED)

    @patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh")
    @patch(
        "apps.channels.hosted_send_tasks.WhatsAppWebClient.send_message",
        return_value={"messageId": "MANUAL-AFTER-LIMIT"},
    )
    def test_manual_agent_message_is_not_blocked_by_automation_pause(
        self,
        provider_send,
        _refresh,
    ):
        self._create_sent_messages(250)
        sync_hosted_health_from_messages(account=self.account)
        manual = self._queued_message(
            raw_payload={"shvya_hosted": {"origin": "agent"}},
            body="Human reply",
        )

        result = send_hosted_whatsapp_message_task.run(str(manual.id))

        self.assertEqual(result["status"], "sent")
        provider_send.assert_called_once()
        manual.refresh_from_db()
        self.assertEqual(manual.status, WhatsAppMessage.Status.SENT)
