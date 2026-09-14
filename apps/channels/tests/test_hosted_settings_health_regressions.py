import json

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_health_guard import (
    reserve_hosted_automation_send,
    sync_hosted_health_from_messages,
)
from services.channels.hosted_whatsapp_service import (
    create_hosted_account,
    get_session_settings,
    handle_gateway_event,
)


class HostedSettingsHealthRegressionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted Regression Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-regression@example.com",
            password="test-password",
            name="Hosted Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Hosted Sales",
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

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def test_ai_and_followup_off_survive_settings_round_trip(self):
        url = reverse("whatsapp-hosted-session-settings", args=[self.account.id])

        response = self.client.post(
            url,
            data=json.dumps(
                {
                    "ai_auto_reply": False,
                    "auto_follow_up": False,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["settings"]["ai_auto_reply"])
        self.assertFalse(response.json()["settings"]["auto_follow_up"])

        self.pipeline.refresh_from_db()
        self.assertFalse(self.pipeline.ai_enabled)

        # Simulate the next page/modal load instead of trusting the POST echo.
        self.account.refresh_from_db()
        self.account.organization.refresh_from_db()
        persisted = get_session_settings(account=self.account)
        self.assertFalse(persisted["ai_auto_reply"])
        self.assertFalse(persisted["auto_follow_up"])

        get_response = self.client.get(url)
        self.assertEqual(get_response.status_code, 200)
        self.assertFalse(get_response.json()["settings"]["ai_auto_reply"])
        self.assertFalse(get_response.json()["settings"]["auto_follow_up"])

    def test_realtime_outbound_from_linked_app_is_counted_by_account_health(self):
        message = handle_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "message",
                "messageId": "DIRECT-APP-1",
                "fromMe": True,
                "from": "918700274739@c.us",
                "to": "919876543210@c.us",
                "chatId": "919876543210@c.us",
                "body": "Sent from the linked WhatsApp app",
                "messageType": "text",
                "status": "sent",
                "isGroup": False,
            }
        )
        self.assertIsNotNone(message)
        self.assertEqual(message.direction, WhatsAppMessage.Direction.OUTBOUND)

        health = sync_hosted_health_from_messages(account=self.account)
        self.assertEqual(health.window_messages_sent, 1)
        self.assertEqual(health.total_messages_sent, 1)

    def test_health_blocks_automation_after_actual_250_message_window(self):
        rows = []
        for index in range(250):
            rows.append(
                WhatsAppMessage(
                    organization=self.org,
                    account=self.account,
                    direction=WhatsAppMessage.Direction.OUTBOUND,
                    external_id=f"wweb:direct-{index}",
                    from_number="+918700274739",
                    to_number=f"+9198765{index:05d}",
                    body="Direct linked-app send",
                    message_type=WhatsAppMessage.MessageType.TEXT,
                    status=WhatsAppMessage.Status.SENT,
                    raw_payload={"fromMe": True, "isHistory": False},
                )
            )
        WhatsAppMessage.objects.bulk_create(rows)

        health = sync_hosted_health_from_messages(account=self.account)
        self.assertEqual(health.window_messages_sent, 250)
        self.assertIsNotNone(health.paused_until)

        gate = reserve_hosted_automation_send(account=self.account)
        self.assertFalse(gate["reserved"])
        self.assertIsNotNone(gate["blocked_until"])

    def test_final_allowed_slot_closes_gate_before_concurrent_message_251(self):
        rows = []
        for index in range(249):
            rows.append(
                WhatsAppMessage(
                    organization=self.org,
                    account=self.account,
                    direction=WhatsAppMessage.Direction.OUTBOUND,
                    external_id=f"wweb:prelimit-{index}",
                    from_number="+918700274739",
                    to_number=f"+9199765{index:05d}",
                    body="Pre-limit send",
                    message_type=WhatsAppMessage.MessageType.TEXT,
                    status=WhatsAppMessage.Status.SENT,
                    raw_payload={"fromMe": True, "isHistory": False},
                )
            )
        WhatsAppMessage.objects.bulk_create(rows)

        first = reserve_hosted_automation_send(account=self.account)
        second = reserve_hosted_automation_send(account=self.account)

        self.assertTrue(first["reserved"])
        self.assertIsNone(first["blocked_until"])
        self.assertFalse(second["reserved"])
        self.assertIsNotNone(second["blocked_until"])
