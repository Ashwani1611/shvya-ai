import json
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp_web import WhatsAppWebGatewayError
from apps.crm.models import Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_automation_service import HostedAutomationPaused
from services.channels.hosted_whatsapp_service import create_hosted_account
from services.channels.hosted_whatsapp_transport import send_hosted_message


class HostedFollowupMediaInboxTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted Follow-up Media Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-media@example.com",
            password="test-password",
            name="Hosted Media Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Hosted Media Sales",
            country_code="+91",
            phone_number="9319988591",
            owner=self.user,
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
            phone_number="9319988591",
        )
        self.account.status = WhatsAppAccount.Status.CONNECTED
        self.account.save(update_fields=["status", "updated_at"])

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def _message(self, *, external_id, peer, body="Hello", message_type="text", raw=None):
        payload = {
            "messageId": external_id.replace("wweb:", ""),
            "from": f"{peer.lstrip('+')}@c.us" if peer.startswith("+") else peer,
            "to": self.account.display_phone_number,
            "fromMe": False,
            "body": body,
            "messageType": message_type,
            "chatId": peer,
            "rawChatId": peer,
            "peerKey": peer,
            "peerPhone": peer if peer.startswith("+") else "",
            "contactName": "Customer",
            "chatName": "Customer",
            "isGroup": False,
            **(raw or {}),
        }
        return WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            direction=WhatsAppMessage.Direction.INBOUND,
            from_number=peer,
            to_number=self.account.display_phone_number,
            body=body,
            message_type=message_type,
            status=WhatsAppMessage.Status.RECEIVED,
            external_id=external_id,
            raw_payload=payload,
        )

    @patch("apps.channels.hosted_chat_ui._request_history_refresh", return_value=False)
    @patch("apps.channels.hosted_chat_ui._repair_live_status")
    def test_opening_inbox_keeps_right_pane_empty_and_hides_status(self, _repair, _refresh):
        self._message(external_id="wweb:NORMAL-1", peer="+919811112222")
        self._message(
            external_id="wweb:STATUS-1",
            peer="status@broadcast",
            body="WhatsApp status",
            raw={"isStatus": True, "chatId": "status@broadcast", "rawChatId": "status@broadcast"},
        )

        data_response = self.client.get(
            reverse("whatsapp-hosted-session-chats-data", args=[self.account.id])
        )
        self.assertEqual(data_response.status_code, 200)
        payload = data_response.json()
        self.assertEqual(payload["selected_chat"], "")
        self.assertEqual(payload["thread"], [])
        self.assertEqual(len(payload["conversations"]), 1)
        self.assertEqual(payload["conversations"][0]["key"], "+919811112222")

        page_response = self.client.get(
            reverse("whatsapp-hosted-session-chats", args=[self.account.id])
        )
        self.assertContains(page_response, "Your conversations")
        self.assertContains(
            page_response,
            "Select a chat from the left to read messages and manage the lead without leaving the inbox.",
        )

    @patch("apps.channels.hosted_chat_ui.config", return_value="callback-token")
    def test_status_callback_is_ignored_before_persistence(self, _config):
        response = self.client.post(
            reverse("whatsapp-hosted-gateway-event"),
            data=json.dumps(
                {
                    "sessionId": str(self.account.id),
                    "event": "message",
                    "messageId": "STATUS-LIVE-1",
                    "from": "status@broadcast",
                    "to": "919319988591@c.us",
                    "fromMe": False,
                    "body": "status content",
                    "messageType": "image",
                    "chatId": "status@broadcast",
                    "rawChatId": "status@broadcast",
                    "peerKey": "status@broadcast",
                    "isStatus": True,
                }
            ),
            content_type="application/json",
            HTTP_X_SHVYA_HOSTED_TOKEN="callback-token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["handled"])
        self.assertEqual(
            WhatsAppMessage.objects.filter(external_id="wweb:STATUS-LIVE-1").count(),
            0,
        )

    @patch("apps.channels.hosted_chat_ui._request_history_refresh", return_value=False)
    @patch("apps.channels.hosted_chat_ui._repair_live_status")
    def test_media_thread_serializes_authenticated_media_urls(self, _repair, _refresh):
        message = self._message(
            external_id="wweb:MEDIA-IMAGE-1",
            peer="+919822223333",
            body="Photo caption",
            message_type=WhatsAppMessage.MessageType.IMAGE,
        )
        response = self.client.get(
            reverse("whatsapp-hosted-session-chats-data", args=[self.account.id]),
            {"chat": "+919822223333"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["thread"]), 1)
        row = payload["thread"][0]
        expected = reverse(
            "whatsapp-hosted-session-chat-media",
            args=[self.account.id, message.id],
        )
        self.assertEqual(row["media_url"], expected)
        self.assertEqual(row["media_download_url"], f"{expected}?download=1")

    @patch("apps.channels.hosted_chat_ui.WhatsAppWebClient.download_message_media")
    def test_media_proxy_renders_inline_and_supports_download(self, download_media):
        message = self._message(
            external_id="wweb:MEDIA-PROXY-1",
            peer="+919844445555",
            message_type=WhatsAppMessage.MessageType.IMAGE,
        )
        download_media.return_value = {
            "content": b"fake-jpeg-content",
            "content_type": "image/jpeg",
            "filename": "photo.jpg",
        }
        url = reverse(
            "whatsapp-hosted-session-chat-media",
            args=[self.account.id, message.id],
        )

        inline = self.client.get(url)
        self.assertEqual(inline.status_code, 200)
        self.assertEqual(inline["Content-Type"], "image/jpeg")
        self.assertTrue(inline["Content-Disposition"].startswith("inline;"))
        self.assertEqual(inline.content, b"fake-jpeg-content")

        download = self.client.get(url, {"download": "1"})
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download["Content-Disposition"].startswith("attachment;"))

    @patch("services.channels.hosted_automation_service.automation_pause_until", return_value=None)
    @patch("apps.channels.providers.whatsapp_web.WhatsAppWebClient.send_message")
    def test_transient_gateway_failure_defers_automation_instead_of_permanent_pause(
        self,
        send_message,
        _pause,
    ):
        send_message.side_effect = WhatsAppWebGatewayError(
            "temporary gateway error",
            status_code=502,
        )
        message = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.display_phone_number,
            to_number="+919877776666",
            body="Automated follow-up",
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload={
                "shvya_auto_followup": {
                    "provider": "hosted",
                    "sequence_id": "test-sequence",
                }
            },
        )

        with self.assertRaises(HostedAutomationPaused):
            send_hosted_message(message=message, defer_on_pause=True)

        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertIn("temporary gateway error", message.error)
