from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core import signing
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.instagram_models import (
    InstagramAccount,
    InstagramConversation,
    InstagramMessage,
    InstagramOAuthAttempt,
)
from apps.channels.instagram_ui import OAUTH_STATE_SALT
from apps.organizations.models import Organization
from services.channels.instagram_service import get_connection, save_connection


@override_settings(META_APP_ID="meta-app-123", META_APP_SECRET="meta-secret-123")
class InstagramUITests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Instagram Org")
        self.user = User.objects.create_user(
            email="instagram@example.com",
            password="test-password",
            name="Instagram Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def connect_instagram(self, organization=None, **overrides):
        organization = organization or self.org
        values = {
            "access_token": "instagram-secret-token",
            "ig_user_id": "17841400000000000",
            "username": "shvya_test",
            "display_name": "SHVYA Test",
            "profile_picture_url": "https://example.com/profile.jpg",
            "expires_in": 3600,
        }
        values.update(overrides)
        save_connection(organization, **values)
        account = InstagramAccount.objects.get(organization=organization)
        account.last_sync_at = timezone.now()
        account.save(update_fields=["last_sync_at", "updated_at"])
        return account

    def add_conversation(self, account, **overrides):
        values = {
            "organization": account.organization,
            "account": account,
            "meta_conversation_id": "conversation-meta-1",
            "participant_id": "ig-scoped-1",
            "participant_username": "buyer_one",
            "participant_name": "Buyer One",
            "last_message_text": "Is this available?",
            "last_message_at": timezone.now(),
            "last_direction": "inbound",
        }
        values.update(overrides)
        return InstagramConversation.objects.create(**values)

    def test_connect_page_replaces_coming_soon(self):
        response = self.client.get(reverse("crm-instagram-connect"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "channels/instagram_connect.html")
        self.assertContains(response, "Continue with Instagram")
        self.assertContains(response, "Your DMs")

    def test_oauth_start_uses_current_instagram_business_scopes(self):
        response = self.client.get(reverse("crm-instagram-oauth-start"))
        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response["Location"])
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "www.instagram.com")
        self.assertEqual(params["client_id"], ["meta-app-123"])
        scopes = params["scope"][0].split(",")
        self.assertIn("instagram_business_basic", scopes)
        self.assertIn("instagram_business_manage_messages", scopes)
        self.assertTrue(params.get("state", [""])[0])

    @patch("apps.channels.instagram_ui.complete_instagram_oauth_task.delay")
    def test_oauth_callback_persists_encrypted_attempt_and_queues_worker(self, delay):
        state = signing.dumps(
            {
                "organization_id": str(self.org.id),
                "user_id": str(self.user.id),
            },
            salt=OAUTH_STATE_SALT,
            compress=True,
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.get(
                reverse("crm-instagram-oauth-return"),
                {"state": state, "code": "one-time-meta-code"},
            )
        self.assertRedirects(response, reverse("crm-instagram-connect"))
        attempt = InstagramOAuthAttempt.objects.get(organization=self.org)
        self.assertEqual(attempt.status, InstagramOAuthAttempt.Status.QUEUED)
        self.assertEqual(attempt.authorization_code, "one-time-meta-code")
        delay.assert_called_once_with(str(attempt.id))

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT authorization_code FROM channels_instagramoauthattempt WHERE id = %s",
                [attempt.id],
            )
            raw_value = cursor.fetchone()[0]
        self.assertNotIn("one-time-meta-code", raw_value)

    def test_connection_token_is_encrypted_and_organization_scoped(self):
        account = self.connect_instagram()
        self.assertEqual(get_connection(self.org)["username"], "shvya_test")
        self.assertEqual(account.access_token, "instagram-secret-token")

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT access_token FROM channels_instagramaccount WHERE id = %s",
                [account.id],
            )
            raw_value = cursor.fetchone()[0]
        self.assertNotIn("instagram-secret-token", raw_value)

        other = Organization.objects.create(name="Other Instagram Org")
        self.assertIsNone(get_connection(other))

    def test_chats_redirect_to_connect_when_account_is_missing(self):
        response = self.client.get(reverse("crm-instagram-chats"))
        self.assertRedirects(response, reverse("crm-instagram-connect"))

    def test_connected_inbox_reads_persisted_conversation(self):
        account = self.connect_instagram()
        self.add_conversation(account)
        response = self.client.get(reverse("crm-instagram-chats"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "channels/instagram_chat_list.html")
        self.assertContains(response, "Buyer One")
        self.assertContains(response, "Is this available?")

    def test_chat_detail_is_tenant_scoped_and_marks_inbound_read(self):
        account = self.connect_instagram()
        conversation = self.add_conversation(account)
        message = InstagramMessage.objects.create(
            organization=self.org,
            account=account,
            conversation=conversation,
            external_id="ig-message-1",
            direction=InstagramMessage.Direction.INBOUND,
            status=InstagramMessage.Status.RECEIVED,
            sender_id="ig-scoped-1",
            recipient_id=account.ig_user_id,
            body="Hello from Instagram",
            is_read=False,
            sent_at=timezone.now(),
        )
        conversation.unread_count = 1
        conversation.save(update_fields=["unread_count", "updated_at"])

        response = self.client.get(
            reverse("crm-instagram-chat-detail", args=[conversation.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hello from Instagram")
        message.refresh_from_db()
        conversation.refresh_from_db()
        self.assertTrue(message.is_read)
        self.assertEqual(conversation.unread_count, 0)

        other = Organization.objects.create(name="Other Org")
        other_account = self.connect_instagram(
            organization=other,
            ig_user_id="17841400000000001",
            username="other_account",
        )
        other_conversation = self.add_conversation(
            other_account,
            meta_conversation_id="other-meta",
            participant_id="other-user",
        )
        response = self.client.get(
            reverse("crm-instagram-chat-detail", args=[other_conversation.id])
        )
        self.assertRedirects(response, reverse("crm-instagram-chats"))

    @patch("apps.channels.instagram_ui.send_instagram_message_task.delay")
    def test_send_message_queues_scoped_outbound_row(self, delay):
        account = self.connect_instagram()
        conversation = self.add_conversation(account)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("crm-instagram-send-message", args=[conversation.id]),
                {"body": "Thanks for messaging us."},
            )
        self.assertRedirects(
            response,
            reverse("crm-instagram-chat-detail", args=[conversation.id]),
        )
        outbound = InstagramMessage.objects.get(
            conversation=conversation,
            direction=InstagramMessage.Direction.OUTBOUND,
        )
        self.assertEqual(outbound.status, InstagramMessage.Status.QUEUED)
        self.assertEqual(outbound.recipient_id, "ig-scoped-1")
        self.assertEqual(outbound.body, "Thanks for messaging us.")
        delay.assert_called_once_with(str(outbound.id))

    @patch("apps.channels.instagram_ui.disconnect_instagram_account_task.delay")
    def test_disconnect_is_workspace_scoped_and_queued(self, delay):
        account = self.connect_instagram()
        other = Organization.objects.create(name="Other Instagram Org")
        self.connect_instagram(
            organization=other,
            ig_user_id="17841400000000001",
            username="other_account",
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("crm-instagram-disconnect"))
        self.assertRedirects(response, reverse("crm-instagram-connect"))
        account.refresh_from_db()
        self.assertEqual(account.status, InstagramAccount.Status.DISCONNECTED)
        self.assertEqual(get_connection(other)["username"], "other_account")
        delay.assert_called_once_with(str(account.id))
