from unittest.mock import Mock, patch

from django.test import TestCase

from apps.organizations.models import Organization
from services.channels.instagram_service import (
    GRAPH_API_BASE,
    InstagramAPIError,
    get_conversation,
    list_conversations,
    save_connection,
    send_text_message,
)


class InstagramServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Instagram Service Org")
        save_connection(
            self.org,
            access_token="secret-token",
            ig_user_id="ig-business-1",
            username="shvya_business",
        )

    @patch("services.channels.instagram_service.requests.get")
    def test_list_conversations_maps_participant_and_latest_message(self, request_get):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "data": [
                {
                    "id": "conversation-1",
                    "updated_time": "2026-09-14T12:30:00+0000",
                    "participants": {
                        "data": [
                            {"id": "ig-business-1", "username": "shvya_business"},
                            {"id": "ig-scoped-1", "username": "buyer_one", "name": "Buyer One"},
                        ]
                    },
                    "messages": {
                        "data": [
                            {
                                "id": "message-1",
                                "created_time": "2026-09-14T12:30:00+0000",
                                "from": {"id": "ig-scoped-1"},
                                "message": "Hello from Instagram",
                            }
                        ]
                    },
                }
            ]
        }
        request_get.return_value = response

        conversations = list_conversations(self.org)
        self.assertEqual(len(conversations), 1)
        self.assertEqual(conversations[0]["participant_id"], "ig-scoped-1")
        self.assertEqual(conversations[0]["participant_name"], "Buyer One")
        self.assertEqual(conversations[0]["last_message"], "Hello from Instagram")
        self.assertEqual(conversations[0]["last_direction"], "inbound")
        self.assertTrue(request_get.call_args.args[0].startswith(GRAPH_API_BASE))
        self.assertEqual(
            request_get.call_args.kwargs["headers"]["Authorization"],
            "Bearer secret-token",
        )

    @patch("services.channels.instagram_service.requests.get")
    def test_get_conversation_sorts_messages_and_marks_direction(self, request_get):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "id": "conversation-1",
            "participants": {
                "data": [
                    {"id": "ig-business-1", "username": "shvya_business"},
                    {"id": "ig-scoped-1", "username": "buyer_one"},
                ]
            },
            "messages": {
                "data": [
                    {
                        "id": "message-2",
                        "created_time": "2026-09-14T12:31:00+0000",
                        "from": {"id": "ig-business-1"},
                        "message": "Our reply",
                    },
                    {
                        "id": "message-1",
                        "created_time": "2026-09-14T12:30:00+0000",
                        "from": {"id": "ig-scoped-1"},
                        "message": "Customer message",
                    },
                ]
            },
        }
        request_get.return_value = response

        conversation = get_conversation(self.org, "conversation-1")
        self.assertEqual(conversation["participant_id"], "ig-scoped-1")
        self.assertEqual(
            [message["id"] for message in conversation["messages"]],
            ["message-1", "message-2"],
        )
        self.assertEqual(conversation["messages"][0]["direction"], "inbound")
        self.assertEqual(conversation["messages"][1]["direction"], "outbound")

    @patch("services.channels.instagram_service.requests.post")
    def test_send_text_message_posts_to_connected_instagram_identity(self, request_post):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "recipient_id": "ig-scoped-1",
            "message_id": "message-1",
        }
        request_post.return_value = response

        result = send_text_message(
            self.org,
            recipient_id="ig-scoped-1",
            body="Hello from SHVYA",
        )
        self.assertEqual(result["message_id"], "message-1")
        self.assertEqual(
            request_post.call_args.kwargs["json"],
            {
                "recipient": {"id": "ig-scoped-1"},
                "message": {"text": "Hello from SHVYA"},
            },
        )
        self.assertIn("/ig-business-1/messages", request_post.call_args.args[0])

    @patch("services.channels.instagram_service.requests.post")
    def test_meta_error_is_exposed_as_safe_service_error(self, request_post):
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.text = ""
        response.json.return_value = {"error": {"message": "Permission denied"}}
        request_post.return_value = response

        with self.assertRaisesMessage(InstagramAPIError, "Permission denied"):
            send_text_message(
                self.org,
                recipient_id="ig-scoped-1",
                body="Hello",
            )
