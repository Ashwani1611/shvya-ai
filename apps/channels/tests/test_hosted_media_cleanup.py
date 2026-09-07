import base64
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline
from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import create_hosted_account


class HostedMediaCleanupTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted Media Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-media@example.com",
            password="test-password",
            name="Hosted Media Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        Pipeline.objects.create(
            organization=self.org,
            name="Hosted Media Sales",
            country_code="+91",
            phone_number="9876501234",
            owner=self.user,
        )
        self.account, _pipeline, _created = create_hosted_account(
            organization=self.org,
            created_by=self.user,
            country_code="+91",
            phone_number="9876501234",
        )
        self.account.status = WhatsAppAccount.Status.CONNECTED
        self.account.save(update_fields=["status", "updated_at"])

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def _media_message(self):
        return WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wweb:MEDIA-1",
            from_number="+919811112222",
            to_number=self.account.display_phone_number,
            body="Photo caption",
            message_type=WhatsAppMessage.MessageType.IMAGE,
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload={
                "hasMedia": True,
                "rawMessageType": "image",
                "peerPhone": "+919811112222",
            },
        )

    @patch("apps.channels.hosted_media_ui.WhatsAppWebClient.download_media")
    def test_media_proxy_returns_private_gateway_media(self, download_media):
        message = self._media_message()
        download_media.return_value = {
            "data": base64.b64encode(b"image-bytes").decode("ascii"),
            "mimetype": "image/png",
            "filename": "photo.png",
        }

        response = self.client.get(
            reverse(
                "whatsapp-hosted-message-media",
                args=[self.account.id, message.id],
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"image-bytes")
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        download_media.assert_called_once_with(
            session_id=self.account.id,
            message_id="MEDIA-1",
        )

    def test_chat_data_contains_authenticated_media_url(self):
        message = self._media_message()
        response = self.client.get(
            reverse("whatsapp-hosted-session-chats-data", args=[self.account.id]),
            {"chat": "+919811112222"},
        )

        self.assertEqual(response.status_code, 200)
        thread = response.json()["thread"]
        row = next(item for item in thread if item["id"] == str(message.id))
        self.assertTrue(row["has_media"])
        self.assertEqual(row["raw_message_type"], "image")
        self.assertEqual(
            row["media_url"],
            reverse(
                "whatsapp-hosted-message-media",
                args=[self.account.id, message.id],
            ),
        )

    @patch("apps.channels.hosted_remove_ui.remove_hosted_session_task.delay")
    def test_remove_session_hides_account_and_queues_gateway_cleanup(self, delay):
        response = self.client.post(
            reverse("whatsapp-hosted-session-remove", args=[self.account.id])
        )

        self.assertEqual(response.status_code, 200)
        self.account.refresh_from_db()
        self.assertFalse(self.account.is_active)
        self.assertEqual(self.account.status, WhatsAppAccount.Status.DISCONNECTED)
        delay.assert_called_once_with(str(self.account.id))
