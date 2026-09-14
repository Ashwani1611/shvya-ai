from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
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
        return save_connection(organization, **values)

    def test_connect_page_replaces_coming_soon(self):
        response = self.client.get(reverse("crm-instagram-connect"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "channels/instagram_connect.html")
        self.assertContains(response, "Continue with Instagram")
        self.assertContains(response, "Your DMs")

    def test_oauth_start_is_bound_to_instagram_business_scopes(self):
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

    def test_connection_token_is_encrypted_and_organization_scoped(self):
        self.connect_instagram()
        self.org.refresh_from_db()

        stored = self.org.settings["instagram"]
        self.assertEqual(stored["status"], "connected")
        self.assertEqual(stored["username"], "shvya_test")
        self.assertNotIn("instagram-secret-token", stored["access_token_encrypted"])
        self.assertEqual(
            get_connection(self.org, include_token=True)["access_token"],
            "instagram-secret-token",
        )

        other = Organization.objects.create(name="Other Instagram Org")
        self.assertIsNone(get_connection(other))

    def test_chats_redirect_to_connect_when_account_is_missing(self):
        response = self.client.get(reverse("crm-instagram-chats"))
        self.assertRedirects(response, reverse("crm-instagram-connect"))

    @patch("apps.channels.instagram_ui.list_conversations")
    def test_connected_inbox_renders_conversation_sidepanel(self, list_conversations):
        self.connect_instagram()
        list_conversations.return_value = [
            {
                "id": "conversation-1",
                "participant_id": "ig-scoped-1",
                "participant_username": "buyer_one",
                "participant_name": "Buyer One",
                "last_message": "Is this available?",
                "last_message_at": "2026-09-14T12:30:00+0000",
                "last_direction": "inbound",
            }
        ]

        response = self.client.get(reverse("crm-instagram-chats"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "channels/instagram_chat_list.html")
        self.assertContains(response, "Buyer One")
        self.assertContains(response, "Is this available?")
        list_conversations.assert_called_once()
        self.assertEqual(list_conversations.call_args.args[0].pk, self.org.pk)

    @patch("apps.channels.instagram_ui.list_conversations")
    @patch("apps.channels.instagram_ui.get_conversation")
    def test_chat_detail_is_loaded_for_current_workspace(
        self,
        get_conversation_mock,
        list_conversations_mock,
    ):
        self.connect_instagram()
        list_conversations_mock.return_value = []
        get_conversation_mock.return_value = {
            "id": "conversation-1",
            "participant_id": "ig-scoped-1",
            "participant_username": "buyer_one",
            "participant_name": "Buyer One",
            "messages": [
                {
                    "id": "message-1",
                    "body": "Hello from Instagram",
                    "created_time": "2026-09-14T12:30:00+0000",
                    "direction": "inbound",
                    "attachments": {},
                }
            ],
        }

        response = self.client.get(
            reverse("crm-instagram-chat-detail", args=["conversation-1"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hello from Instagram")
        organization_arg = get_conversation_mock.call_args.args[0]
        self.assertEqual(organization_arg.pk, self.org.pk)

    @patch("apps.channels.instagram_ui.send_text_message")
    @patch("apps.channels.instagram_ui.get_conversation")
    def test_send_message_uses_scoped_recipient(
        self,
        get_conversation_mock,
        send_text_message_mock,
    ):
        self.connect_instagram()
        get_conversation_mock.return_value = {
            "id": "conversation-1",
            "participant_id": "ig-scoped-1",
            "participant_username": "buyer_one",
            "participant_name": "Buyer One",
            "messages": [],
        }

        response = self.client.post(
            reverse("crm-instagram-send-message", args=["conversation-1"]),
            {"body": "Thanks for messaging us."},
        )
        self.assertRedirects(
            response,
            reverse("crm-instagram-chat-detail", args=["conversation-1"]),
        )
        send_text_message_mock.assert_called_once()
        call = send_text_message_mock.call_args
        self.assertEqual(call.args[0].pk, self.org.pk)
        self.assertEqual(call.kwargs["recipient_id"], "ig-scoped-1")
        self.assertEqual(call.kwargs["body"], "Thanks for messaging us.")

    def test_disconnect_removes_only_current_workspace_connection(self):
        self.connect_instagram()
        other = Organization.objects.create(name="Other Instagram Org")
        self.connect_instagram(
            organization=other,
            ig_user_id="17841400000000001",
            username="other_account",
        )

        response = self.client.post(reverse("crm-instagram-disconnect"))
        self.assertRedirects(response, reverse("crm-instagram-connect"))
        self.org.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNone(get_connection(self.org))
        self.assertEqual(get_connection(other)["username"], "other_account")
