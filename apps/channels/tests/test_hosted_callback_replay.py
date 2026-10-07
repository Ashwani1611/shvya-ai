import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from apps.channels.models import WhatsAppAccount
from apps.organizations.models import Organization
from services.channels.hosted_chat_service import handle_hosted_gateway_event


class HostedGatewayCallbackReplayTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted callback replay",
            package="dfy",
            settings={"hosted_account_enabled": True},
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type="hosted",
            phone_number_id="+919000009991",
            display_phone_number="+919000009991",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.url = reverse("whatsapp-hosted-gateway-event")

    def post(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_SHVYA_HOSTED_TOKEN="callback-token",
        )

    @patch("apps.channels.hosted_chat_ui.sync_hosted_contact_names")
    @patch("apps.channels.hosted_chat_ui.repair_content_after_gateway_event")
    @patch("apps.channels.hosted_chat_ui.handle_hosted_gateway_event", return_value=None)
    @patch("apps.channels.hosted_chat_ui.config", return_value="callback-token")
    def test_uncommitted_live_events_return_retryable_non_2xx(
        self, _config, handle, _repair, _sync
    ):
        cases = (
            {"event": "message", "messageId": "inbound-1"},
            {"event": "message_ack", "messageId": "outbound-1", "status": "delivered"},
            {"event": "ready"},
            {"event": "running"},
        )
        for values in cases:
            with self.subTest(event=values["event"]):
                response = self.post(
                    {
                        "sessionId": str(self.account.id),
                        "gatewayShard": "primary",
                        "gatewayOwner": "gateway-new",
                        **values,
                    }
                )
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.json()["handled"])
                self.assertTrue(response.json()["retryable"])
        self.assertEqual(handle.call_count, len(cases))
        _repair.assert_not_called()
        _sync.assert_not_called()

    @patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh")
    @patch("services.channels.hosted_chat_service.repair_gateway_message_identity")
    @patch("services.channels.hosted_chat_service.legacy_handle_gateway_event", return_value=None)
    def test_rejected_history_event_does_not_run_identity_repairs(self, _handle, repair, refresh):
        # A stale gateway still has message IDs from before a session moved.
        # Its failed lease check must also fence the enrichment layer.
        with patch("services.channels.hosted_chat_service.WhatsAppMessage.objects") as messages:
            result = handle_hosted_gateway_event(payload={
                "sessionId": str(self.account.id), "event": "history_sync",
                "messages": [{"messageId": "stale-history"}],
            })
        self.assertIsNone(result)
        messages.select_related.assert_not_called()
        repair.assert_not_called()
        refresh.assert_not_called()

    @patch("apps.channels.hosted_chat_ui.config", return_value="callback-token")
    def test_callback_requires_json_object(self, _config):
        for payload in ([], "message", None, 42):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)

    @patch("apps.channels.hosted_chat_ui.sync_hosted_contact_names")
    @patch("apps.channels.hosted_chat_ui.repair_content_after_gateway_event")
    @patch("apps.channels.hosted_chat_ui.handle_hosted_gateway_event", return_value=None)
    @patch("apps.channels.hosted_chat_ui.config", return_value="callback-token")
    def test_unknown_session_does_not_poison_gateway_outbox(
        self, _config, _handle, _repair, _sync
    ):
        response = self.post(
            {
                "sessionId": "00000000-0000-0000-0000-000000000001",
                "event": "message",
                "messageId": "unknown-session-message",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["handled"])
        self.assertNotIn("retryable", response.json())

    @patch("apps.channels.hosted_chat_ui.handle_hosted_gateway_event")
    @patch("apps.channels.hosted_chat_ui.config", return_value="callback-token")
    def test_hidden_system_chat_remains_intentionally_acknowledged(
        self, _config, handle
    ):
        response = self.post(
            {
                "sessionId": str(self.account.id),
                "event": "message",
                "messageId": "status-message",
                "from": "status@broadcast",
                "chatId": "status@broadcast",
                "rawChatId": "status@broadcast",
                "peerKey": "status@broadcast",
                "isStatus": True,
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["handled"])
        self.assertEqual(response.json()["ignored"], "system_chat")
        handle.assert_not_called()

    @patch("apps.channels.hosted_chat_ui.sync_hosted_contact_names")
    @patch("apps.channels.hosted_chat_ui.repair_content_after_gateway_event")
    @patch("apps.channels.hosted_chat_ui.handle_hosted_gateway_event")
    @patch("apps.channels.hosted_chat_ui.config", return_value="callback-token")
    def test_committed_live_event_is_acknowledged_once(
        self, _config, handle, _repair, _sync
    ):
        handle.return_value = self.account
        response = self.post(
            {
                "sessionId": str(self.account.id),
                "event": "message",
                "messageId": "committed-message",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["handled"])
        self.assertNotIn("retryable", response.json())

    @patch("apps.channels.hosted_chat_ui.sync_hosted_contact_names")
    @patch("apps.channels.hosted_chat_ui.repair_content_after_gateway_event")
    @patch("apps.channels.hosted_chat_ui.handle_hosted_gateway_event", return_value=None)
    @patch("apps.channels.hosted_chat_ui.config", return_value="callback-token")
    def test_malformed_live_event_is_not_retried_forever(
        self, _config, _handle, _repair, _sync
    ):
        response = self.post(
            {
                "sessionId": str(self.account.id),
                "event": "message_ack",
                "status": "sent",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["handled"])
        self.assertNotIn("retryable", response.json())
