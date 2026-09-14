import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.instagram_models import (
    InstagramAccount,
    InstagramConversation,
    InstagramMessage,
    InstagramOAuthAttempt,
    InstagramWebhookDelivery,
)
from apps.organizations.models import Organization
from services.channels.instagram_service import (
    InstagramAPIError,
    complete_oauth_attempt,
    process_webhook_delivery,
    queue_text_message,
    refresh_account_token,
    save_connection,
    send_queued_message,
    subscribe_account_webhooks,
    sync_account_conversations,
)


def meta_response(payload, status=200):
    response = Mock()
    response.ok = 200 <= status < 300
    response.status_code = status
    response.text = ""
    response.json.return_value = payload
    return response


@override_settings(META_APP_ID="meta-app-123", META_APP_SECRET="meta-secret-123")
class InstagramServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Instagram Service Org")
        save_connection(
            self.org,
            access_token="secret-token",
            ig_user_id="ig-business-1",
            username="shvya_business",
            expires_in=3600,
        )
        self.account = InstagramAccount.objects.get(organization=self.org)

    def add_conversation(self):
        return InstagramConversation.objects.create(
            organization=self.org,
            account=self.account,
            meta_conversation_id="conversation-1",
            participant_id="ig-scoped-1",
            participant_username="buyer_one",
            participant_name="Buyer One",
            last_message_at=timezone.now(),
        )

    @patch("services.channels.instagram_service.requests.request")
    def test_sync_conversations_persists_participant_and_messages(self, request):
        request.side_effect = [
            meta_response(
                {
                    "data": [
                        {
                            "id": "conversation-1",
                            "updated_time": "2026-09-14T12:31:00+0000",
                            "participants": {
                                "data": [
                                    {"id": "ig-business-1", "username": "shvya_business"},
                                    {
                                        "id": "ig-scoped-1",
                                        "username": "buyer_one",
                                        "name": "Buyer One",
                                    },
                                ]
                            },
                        }
                    ],
                    "paging": {"cursors": {}},
                }
            ),
            meta_response(
                {
                    "id": "conversation-1",
                    "updated_time": "2026-09-14T12:31:00+0000",
                    "participants": {
                        "data": [
                            {"id": "ig-business-1", "username": "shvya_business"},
                            {
                                "id": "ig-scoped-1",
                                "username": "buyer_one",
                                "name": "Buyer One",
                            },
                        ]
                    },
                    "messages": {
                        "data": [
                            {
                                "id": "message-2",
                                "created_time": "2026-09-14T12:31:00+0000",
                                "from": {"id": "ig-business-1"},
                                "to": {"data": [{"id": "ig-scoped-1"}]},
                                "message": "Our reply",
                            },
                            {
                                "id": "message-1",
                                "created_time": "2026-09-14T12:30:00+0000",
                                "from": {"id": "ig-scoped-1"},
                                "to": {"data": [{"id": "ig-business-1"}]},
                                "message": "Customer message",
                            },
                        ]
                    },
                }
            ),
        ]

        count = sync_account_conversations(self.account)
        self.assertEqual(count, 1)
        conversation = InstagramConversation.objects.get(account=self.account)
        self.assertEqual(conversation.participant_id, "ig-scoped-1")
        self.assertEqual(conversation.participant_name, "Buyer One")
        self.assertEqual(conversation.last_message_text, "Our reply")
        self.assertEqual(conversation.last_direction, InstagramMessage.Direction.OUTBOUND)
        self.assertEqual(conversation.messages.count(), 2)
        self.assertEqual(
            conversation.messages.get(external_id="message-1").direction,
            InstagramMessage.Direction.INBOUND,
        )
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.last_sync_at)
        self.assertIn("/ig-business-1/conversations", request.call_args_list[0].args[1])

    @patch("services.channels.instagram_service.requests.request")
    def test_send_queued_message_uses_meta_send_api_and_persists_message_id(self, request):
        conversation = self.add_conversation()
        queued = queue_text_message(
            self.org,
            conversation_id=conversation.id,
            body="Hello from SHVYA",
        )
        request.return_value = meta_response(
            {"recipient_id": "ig-scoped-1", "message_id": "meta-message-1"}
        )

        delivered = send_queued_message(queued)
        self.assertEqual(delivered.status, InstagramMessage.Status.SENT)
        self.assertEqual(delivered.external_id, "meta-message-1")
        call = request.call_args
        self.assertEqual(call.args[0], "POST")
        self.assertIn("/ig-business-1/messages", call.args[1])
        self.assertEqual(
            call.kwargs["json"],
            {
                "recipient": {"id": "ig-scoped-1"},
                "message": {"text": "Hello from SHVYA"},
            },
        )

    @patch("services.channels.instagram_service.requests.request")
    def test_subscribe_account_webhooks_uses_required_messaging_fields(self, request):
        request.return_value = meta_response({"success": True})
        subscribe_account_webhooks(self.account)
        self.account.refresh_from_db()
        self.assertTrue(self.account.webhook_subscribed)
        self.assertEqual(
            self.account.subscribed_fields,
            ["messages", "messaging_postbacks"],
        )
        call = request.call_args
        self.assertIn("/ig-business-1/subscribed_apps", call.args[1])
        self.assertEqual(
            call.kwargs["params"]["subscribed_fields"],
            "messages,messaging_postbacks",
        )

    @patch("services.channels.instagram_service.requests.request")
    def test_refresh_long_lived_token_updates_expiry(self, request):
        request.return_value = meta_response(
            {"access_token": "refreshed-token", "expires_in": 5_184_000}
        )
        before = timezone.now()
        refresh_account_token(self.account)
        self.account.refresh_from_db()
        self.assertEqual(self.account.access_token, "refreshed-token")
        self.assertGreater(self.account.token_expires_at, before + timedelta(days=50))
        self.assertIn("/refresh_access_token", request.call_args.args[1])
        self.assertEqual(
            request.call_args.kwargs["params"]["grant_type"],
            "ig_refresh_token",
        )

    @patch("services.channels.instagram_service.requests.request")
    def test_oauth_exchange_stores_account_and_clears_one_time_code(self, request):
        other = Organization.objects.create(name="OAuth Instagram Org")
        user = User.objects.create_user(
            email="oauth-instagram@example.com",
            password="password",
            name="OAuth Admin",
            organization=other,
            role=User.Role.ADMIN,
        )
        attempt = InstagramOAuthAttempt.objects.create(
            organization=other,
            created_by=user,
            authorization_code="temporary-code",
            redirect_uri="https://dashboard.shvya-ai.com/dashboard/instagram/connect/return/",
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        request.side_effect = [
            meta_response({"access_token": "short-token", "user_id": "ig-oauth-1"}),
            meta_response({"access_token": "long-token", "expires_in": 5_184_000}),
            meta_response(
                {
                    "id": "ig-oauth-1",
                    "username": "oauth_business",
                    "name": "OAuth Business",
                    "account_type": "BUSINESS",
                }
            ),
        ]

        account = complete_oauth_attempt(attempt)
        attempt.refresh_from_db()
        self.assertEqual(account.organization, other)
        self.assertEqual(account.access_token, "long-token")
        self.assertEqual(account.username, "oauth_business")
        self.assertEqual(attempt.status, InstagramOAuthAttempt.Status.CONNECTED)
        self.assertEqual(attempt.authorization_code, "")
        self.assertEqual(request.call_args_list[0].args[0], "POST")
        self.assertEqual(
            request.call_args_list[0].kwargs["data"]["grant_type"],
            "authorization_code",
        )
        self.assertEqual(
            request.call_args_list[1].kwargs["params"]["grant_type"],
            "ig_exchange_token",
        )

    @patch("services.channels.instagram_service.requests.request")
    def test_meta_error_keeps_code_and_trace_metadata(self, request):
        request.return_value = meta_response(
            {
                "error": {
                    "message": "The access token has expired",
                    "code": 190,
                    "error_subcode": 463,
                    "fbtrace_id": "trace-1",
                }
            },
            status=400,
        )
        conversation = self.add_conversation()
        queued = queue_text_message(
            self.org,
            conversation_id=conversation.id,
            body="Hello",
        )
        with self.assertRaises(InstagramAPIError) as raised:
            send_queued_message(queued)
        self.assertTrue(raised.exception.token_invalid)
        self.assertEqual(raised.exception.subcode, 463)
        self.assertEqual(raised.exception.fbtrace_id, "trace-1")

    def test_webhook_processor_is_idempotent_and_tenant_scoped(self):
        payload = {
            "object": "instagram",
            "entry": [
                {
                    "id": "ig-business-1",
                    "messaging": [
                        {
                            "sender": {"id": "ig-scoped-1"},
                            "recipient": {"id": "ig-business-1"},
                            "timestamp": 1789392600000,
                            "message": {
                                "mid": "webhook-message-1",
                                "text": "Hello from webhook",
                            },
                        }
                    ],
                }
            ],
        }
        delivery = InstagramWebhookDelivery.objects.create(
            payload_sha256="a" * 64,
            raw_payload=payload,
        )
        with transaction.atomic():
            count = process_webhook_delivery(delivery)
        self.assertEqual(count, 1)
        message = InstagramMessage.objects.get(external_id="webhook-message-1")
        self.assertEqual(message.organization, self.org)
        self.assertEqual(message.direction, InstagramMessage.Direction.INBOUND)
        self.assertFalse(message.is_read)
        self.assertEqual(message.body, "Hello from webhook")

        with transaction.atomic():
            second_count = process_webhook_delivery(delivery)
        self.assertEqual(second_count, 0)
        self.assertEqual(
            InstagramMessage.objects.filter(external_id="webhook-message-1").count(),
            1,
        )


@override_settings(META_VERIFY_TOKEN="instagram-verify", META_APP_SECRET="webhook-secret")
class InstagramWebhookViewTests(TestCase):
    def test_verification_challenge_uses_constant_time_token_check(self):
        response = self.client.get(
            reverse("instagram-webhook"),
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "instagram-verify",
                "hub.challenge": "challenge-123",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"challenge-123")

    def test_invalid_delivery_signature_is_rejected_before_persistence(self):
        response = self.client.post(
            reverse("instagram-webhook"),
            data=b'{"object":"instagram","entry":[]}',
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=invalid",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(InstagramWebhookDelivery.objects.count(), 0)

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_signed_delivery_is_durable_deduplicated_and_queued(self, delay):
        body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")
        signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
        header = f"sha256={signature}"

        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(
                reverse("instagram-webhook"),
                data=body,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=header,
            )
        second = self.client.post(
            reverse("instagram-webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=header,
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(InstagramWebhookDelivery.objects.count(), 1)
        delivery = InstagramWebhookDelivery.objects.get()
        delay.assert_called_once_with(str(delivery.id))
