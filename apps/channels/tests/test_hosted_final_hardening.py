from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline
from apps.organizations.models import Organization
from services.channels.hosted_message_content import (
    decorate_hosted_chat_snapshot,
    repair_content_after_gateway_event,
)
from services.channels.hosted_whatsapp_service import create_hosted_account


class HostedFinalHardeningTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted Hardening Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-hardening@example.com",
            password="test-password",
            name="Hosted Hardening Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        Pipeline.objects.create(
            organization=self.org,
            name="Hosted Hardening Sales",
            country_code="+91",
            phone_number="9876543210",
            owner=self.user,
        )
        self.account, _pipeline, _created = create_hosted_account(
            organization=self.org,
            created_by=self.user,
            country_code="+91",
            phone_number="9876543210",
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    @patch("apps.channels.hosted_ui.WhatsAppWebClient.get_session")
    def test_expired_gateway_session_is_clear_and_marks_account_disconnected(
        self,
        get_session,
    ):
        get_session.return_value = {
            "status": "expired",
            "phoneNumber": "+919876543210",
            "lastError": "QR session expired after waiting too long for a scan.",
        }

        response = self.client.get(
            reverse("whatsapp-hosted-session-status", args=[self.account.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "expired")
        self.assertEqual(response.json()["label"], "QR Expired")
        self.assertIn("expired", response.json()["error"].lower())
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.DISCONNECTED)

    def test_history_refresh_repairs_existing_blank_body(self):
        message = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wweb:RECOVER-BODY-1",
            from_number="+919811112222",
            to_number="+919876543210",
            body="",
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload={"rawMessageType": "chat"},
        )

        repair_content_after_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [
                    {
                        "messageId": "RECOVER-BODY-1",
                        "body": "Recovered historical text",
                        "messageType": "text",
                        "rawMessageType": "chat",
                    }
                ],
            }
        )

        message.refresh_from_db()
        self.assertEqual(message.body, "Recovered historical text")
        self.assertTrue(message.raw_payload["isHistory"])

    def test_history_refresh_repairs_generic_text_type_to_media(self):
        message = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wweb:RECOVER-IMAGE-1",
            from_number="+919811112222",
            to_number="+919876543210",
            body="",
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload={},
        )

        repair_content_after_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [
                    {
                        "messageId": "RECOVER-IMAGE-1",
                        "body": "",
                        "messageType": "image",
                        "rawMessageType": "image",
                        "hasMedia": True,
                    }
                ],
            }
        )

        message.refresh_from_db()
        self.assertEqual(message.message_type, WhatsAppMessage.MessageType.IMAGE)
        self.assertTrue(message.raw_payload["hasMedia"])

    def test_unsupported_raw_type_gets_display_only_placeholder(self):
        message = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wweb:STICKER-1",
            from_number="+919811112222",
            to_number="+919876543210",
            body="",
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload={"rawMessageType": "sticker"},
        )

        snapshot = {"thread": [message]}
        decorate_hosted_chat_snapshot(snapshot)
        self.assertEqual(message.body, "Sticker")

        message.refresh_from_db()
        self.assertEqual(message.body, "")
