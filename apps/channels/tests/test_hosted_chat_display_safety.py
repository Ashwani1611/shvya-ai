from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.channels.models import WhatsAppMessage
from services.channels.hosted_message_content import (
    decorate_hosted_chat_snapshot,
    display_body_for_message,
    looks_like_transport_payload,
)


class HostedChatDisplaySafetyTests(SimpleTestCase):
    def test_media_data_url_is_not_rendered_as_chat_text(self):
        message = SimpleNamespace(
            body="data:image/png;base64," + ("A" * 1200),
            message_type=WhatsAppMessage.MessageType.IMAGE,
        )
        self.assertEqual(display_body_for_message(message), "")

    def test_normal_media_caption_remains_visible(self):
        message = SimpleNamespace(
            body="Here is the signed proposal.",
            message_type=WhatsAppMessage.MessageType.DOCUMENT,
        )
        self.assertEqual(
            display_body_for_message(message),
            "Here is the signed proposal.",
        )

    def test_long_encoded_value_is_detected_as_transport_payload(self):
        self.assertTrue(looks_like_transport_payload("a" * 900))
        self.assertFalse(looks_like_transport_payload("Customer shared a product photo"))

    def test_conversation_preview_replaces_encoded_payload(self):
        snapshot = {
            "thread": [],
            "conversations": [
                {"last_message": "data:application/pdf;base64," + ("Q" * 900)}
            ],
        }
        decorate_hosted_chat_snapshot(snapshot)
        self.assertEqual(snapshot["conversations"][0]["last_message"], "Attachment")
