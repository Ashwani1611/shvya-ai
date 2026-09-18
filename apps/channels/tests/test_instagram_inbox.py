"""Database/HTTP regression coverage for the Instagram inbox repair."""
from datetime import timedelta
import hashlib
import hmac
import json
import uuid
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.instagram_models import InstagramAccount, InstagramConversation, InstagramMessage, InstagramOAuthAttempt, InstagramWebhookDelivery
from apps.channels.instagram_tasks import complete_instagram_oauth_task, disconnect_instagram_account_task, send_instagram_message_task
from apps.channels.instagram_webhook import instagram_webhook_view
from apps.organizations.models import Organization
from services.channels.instagram_inbox import claim_message, conversation_policy, inbox_conversations, inbox_thread, queue_inbox_reply
from services.channels.instagram_service import InstagramAPIError


@override_settings(APP_ENV="testing", META_INSTAGRAM_APP_ID="ig-app", META_INSTAGRAM_APP_SECRET="ig-secret", META_INSTAGRAM_VERIFY_TOKEN="ig-verify", CACHES={"default":{"BACKEND":"django.core.cache.backends.locmem.LocMemCache"}})
class InstagramInboxTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Instagram inbox tests")
        self.user = User.objects.create_user(email="ig-inbox@example.com", password="test-password", name="IG Admin", organization=self.org, role=User.Role.ADMIN)
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key
        self.account = InstagramAccount.objects.create(organization=self.org, ig_user_id="17841400000099999", access_token="IG-private-token", status=InstagramAccount.Status.CONNECTED, webhook_subscribed=True, last_sync_at=timezone.now(), token_expires_at=timezone.now()+timedelta(days=30))
        self.conversation = InstagramConversation.objects.create(organization=self.org, account=self.account, participant_id="customer-123", participant_name="Test Customer")
        self.inbound = self.add_message(sent_at=timezone.now()-timedelta(minutes=5))

    def add_message(self, **values):
        defaults = dict(organization=self.org, account=self.account, conversation=self.conversation, external_id=str(uuid.uuid4()), direction=InstagramMessage.Direction.INBOUND, status=InstagramMessage.Status.RECEIVED, sender_id=self.conversation.participant_id, recipient_id=self.account.ig_user_id, body="Customer question", sent_at=timezone.now())
        defaults.update(values)
        return InstagramMessage.objects.create(**defaults)

    def detail(self, **params):
        return self.client.get(reverse("crm-instagram-chat-detail", args=[self.conversation.pk]), params, HTTP_ACCEPT="application/json")

    def test_newest_provider_timestamp_orders_history_not_import_order(self):
        old = self.add_message(sent_at=timezone.now()-timedelta(days=3), body="Imported older message")
        data = inbox_thread(self.org, self.conversation.pk)
        self.assertEqual(data["messages"][0]["id"], str(old.pk))
        self.assertEqual(data["messages"][-1]["id"], str(self.inbound.pk))

    def test_inbox_latest_preview_ignores_stale_summary(self):
        self.conversation.last_message_text = "Stale text"
        self.conversation.save(update_fields=["last_message_text"])
        self.assertEqual(inbox_conversations(self.org)["conversations"][0]["last_message"], self.inbound.body)

    def test_history_is_bounded_and_cursor_pages_without_duplicates(self):
        base = timezone.now()-timedelta(days=2)
        for i in range(60):
            self.add_message(sent_at=base+timedelta(minutes=i))
        first = inbox_thread(self.org, self.conversation.pk)
        second = inbox_thread(self.org, self.conversation.pk, before=first["before"])
        self.assertEqual(len(first["messages"]), 50)
        self.assertEqual(len(second["messages"]), 11)
        self.assertFalse({m["id"] for m in first["messages"]} & {m["id"] for m in second["messages"]})

    def test_cursor_cannot_cross_conversations(self):
        other = InstagramConversation.objects.create(organization=self.org, account=self.account, participant_id="customer-other")
        from django.core import signing
        from services.channels.instagram_inbox import CURSOR_SALT
        cursor = signing.dumps({"org":str(self.org.pk),"conversation":str(other.pk),"time":timezone.now().isoformat(),"id":str(self.inbound.pk)}, salt=CURSOR_SALT)
        with self.assertRaises(InstagramAPIError):
            inbox_thread(self.org, self.conversation.pk, before=cursor)

    def test_outbound_does_not_extend_customer_window(self):
        self.inbound.sent_at = timezone.now()-timedelta(hours=25)
        self.inbound.save(update_fields=["sent_at"])
        self.add_message(direction=InstagramMessage.Direction.OUTBOUND, sent_at=timezone.now())
        self.assertFalse(conversation_policy(self.conversation)["can_reply"])
        with self.assertRaises(InstagramAPIError):
            queue_inbox_reply(self.org, conversation_id=self.conversation.pk, body="Blocked")

    def test_no_inbound_blocks_reply(self):
        self.inbound.delete()
        with self.assertRaises(InstagramAPIError):
            queue_inbox_reply(self.org, conversation_id=self.conversation.pk, body="Blocked")

    def test_expired_token_blocks_reply(self):
        self.account.token_expires_at = timezone.now()-timedelta(seconds=1)
        self.account.save(update_fields=["token_expires_at"])
        with self.assertRaises(InstagramAPIError):
            queue_inbox_reply(self.org, conversation_id=self.conversation.pk, body="Blocked")

    @override_settings(APP_ENV="staging", OUTBOUND_MESSAGING_ENABLED=False)
    def test_staging_default_outbound_block_is_preserved(self):
        self.assertFalse(conversation_policy(self.conversation)["can_reply"])

    def test_browser_retry_reuses_existing_uuid_row(self):
        key = uuid.uuid4()
        first = queue_inbox_reply(self.org, conversation_id=self.conversation.pk, body="Reply", idempotency_key=key)
        second = queue_inbox_reply(self.org, conversation_id=self.conversation.pk, body="Reply", idempotency_key=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(InstagramMessage.objects.filter(direction="outbound").count(), 1)
        with self.assertRaises(InstagramAPIError):
            queue_inbox_reply(self.org, conversation_id=self.conversation.pk, body="Different", idempotency_key=key)

    def test_claim_is_durable_and_second_task_does_not_send(self):
        message = queue_inbox_reply(self.org, conversation_id=self.conversation.pk, body="Reply")
        self.assertEqual(claim_message(message.pk).pk, message.pk)
        self.assertIsNone(claim_message(message.pk))

    @patch("services.channels.instagram_service.send_queued_message")
    def test_worker_rechecks_window_after_queue_delay(self, send):
        message = queue_inbox_reply(self.org, conversation_id=self.conversation.pk, body="Reply")
        self.inbound.sent_at = timezone.now()-timedelta(hours=25)
        self.inbound.save(update_fields=["sent_at"])
        result = send_instagram_message_task.run(str(message.pk))
        self.assertEqual(result["status"], "failed")
        send.assert_not_called()

    def test_foreign_workspace_cannot_read_or_queue_conversation(self):
        other = Organization.objects.create(name="Foreign workspace")
        with self.assertRaises(InstagramAPIError):
            inbox_thread(other, self.conversation.pk)
        with self.assertRaises(InstagramAPIError):
            queue_inbox_reply(other, conversation_id=self.conversation.pk, body="Blocked")

    @patch("apps.channels.instagram_ui.sync_instagram_account_task.delay")
    def test_json_poll_does_not_start_provider_sync(self, delay):
        self.account.last_sync_at = None
        self.account.save(update_fields=["last_sync_at"])
        response = self.detail()
        self.assertEqual(response.status_code, 200)
        delay.assert_not_called()
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotContains(response, "IG-private-token")
        self.assertNotContains(response, "ig-secret")

    def test_media_html_and_raw_story_context_are_safely_projected(self):
        self.inbound.attachments = [{"type":"video", "payload":{"url":"https://cdn.example.com/v"}}]
        self.inbound.raw_payload = {"message":{"reply_to":{"story":{"url":"https://www.instagram.com/stories/demo/123"}}}, "internal_secret":"should-not-be-returned"}
        self.inbound.body = "<script>alert(1)</script>"
        self.inbound.save(update_fields=["attachments", "raw_payload", "body"])
        response = self.detail()
        message = response.json()["active_conversation"]["messages"][0]
        self.assertIn("<video", message["html"])
        self.assertIn("Reply to story", message["html"])
        self.assertIn("&lt;script&gt;", message["html"])
        self.assertNotIn("<script>", message["html"])
        self.assertNotContains(response, "should-not-be-returned")

    def test_html_uses_whatsapp_api_shell_and_policy(self):
        response = self.client.get(reverse("crm-instagram-chat-detail", args=[self.conversation.pk]))
        self.assertContains(response, "data-shvya-whatsapp-web-shell")
        self.assertContains(response, "Instagram messaging rules")
        self.assertContains(response, "instagram_inbox.js")

    def test_expired_pending_oauth_is_not_an_endless_spinner(self):
        InstagramOAuthAttempt.objects.create(organization=self.org, created_by=self.user, authorization_code="old-code", redirect_uri="https://example.com/return/", expires_at=timezone.now()-timedelta(seconds=1))
        response = self.client.get(reverse("crm-instagram-connect"), HTTP_ACCEPT="application/json")
        self.assertFalse(response.json()["instagram_oauth_pending"])
        self.assertIn("timed out", response.json()["instagram_connection_error"])

    def test_stale_disconnect_does_not_clear_new_connection(self):
        result = disconnect_instagram_account_task.run(str(self.account.pk))
        self.account.refresh_from_db()
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(self.account.access_token, "IG-private-token")

    @patch("services.channels.instagram_service.sync_account_conversations")
    @patch("services.channels.instagram_service.subscribe_account_webhooks", return_value={})
    @patch("services.channels.instagram_service.complete_oauth_attempt")
    def test_subscription_requires_explicit_success(self, complete, subscribe, sync):
        complete.return_value = self.account
        attempt = InstagramOAuthAttempt.objects.create(organization=self.org, created_by=self.user, authorization_code="code", redirect_uri="https://example.com/return/")
        result = complete_instagram_oauth_task.run(str(attempt.pk))
        self.account.refresh_from_db()
        self.assertEqual(result["status"], "connected_with_warning")
        self.assertFalse(self.account.webhook_subscribed)
        sync.assert_not_called()

    def webhook_request(self, payload=None):
        body = json.dumps(payload or {"object":"instagram", "entry":[]}).encode()
        signature = hmac.new(b"ig-secret", body, hashlib.sha256).hexdigest()
        return RequestFactory().post("/webhooks/instagram/", body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256="sha256="+signature)

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_webhook_broker_failure_is_recovered_by_meta_retry(self, delay):
        delay.side_effect = RuntimeError("broker unavailable")
        self.assertEqual(instagram_webhook_view(self.webhook_request()).status_code, 503)
        self.assertEqual(InstagramWebhookDelivery.objects.count(), 1)
        delay.side_effect = None
        self.assertEqual(instagram_webhook_view(self.webhook_request()).status_code, 200)
        self.assertEqual(InstagramWebhookDelivery.objects.count(), 1)
        self.assertEqual(delay.call_count, 2)

    def test_dedicated_verify_token_and_invalid_unicode_signature(self):
        request = RequestFactory().get("/webhooks/instagram/", {"hub.mode":"subscribe", "hub.verify_token":"ig-verify", "hub.challenge":"challenge"})
        self.assertEqual(instagram_webhook_view(request).content, b"challenge")
        request = RequestFactory().post("/webhooks/instagram/", "{}", content_type="application/json", HTTP_X_HUB_SIGNATURE_256="sha256=\u00e9")
        self.assertEqual(instagram_webhook_view(request).status_code, 403)
