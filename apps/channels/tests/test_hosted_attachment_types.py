import tempfile
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.organizations.models import Organization


class HostedAttachmentTypeTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        self.org = Organization.objects.create(
            name="Hosted Attachment Types",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-types@example.com",
            password="test-password",
            name="Hosted Types Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type="hosted",
            phone_number_id="+919000001111",
            display_phone_number="+919000001111",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def tearDown(self):
        self.media_override.disable()
        self.media_dir.cleanup()
        super().tearDown()

    @patch("apps.channels.hosted_send_ui.send_hosted_whatsapp_message_task.delay")
    def test_photo_video_and_document_are_accepted(self, delay):
        cases = [
            ("image", "photo.jpg", "image/jpeg", b"jpeg", WhatsAppMessage.MessageType.IMAGE),
            ("video", "clip.mp4", "video/mp4", b"video", WhatsAppMessage.MessageType.VIDEO),
            ("document", "quote.pdf", "application/pdf", b"pdf", WhatsAppMessage.MessageType.DOCUMENT),
        ]
        for index, (kind, name, mime, content, expected_type) in enumerate(cases):
            with self.subTest(kind=kind):
                upload = SimpleUploadedFile(name, content, content_type=mime)
                response = self.client.post(
                    reverse("whatsapp-hosted-session-chat-media-send", args=[self.account.id]),
                    {
                        "chat": f"+91980000000{index}",
                        "message_type": kind,
                        "caption": "attachment caption",
                        "attachment": upload,
                    },
                )
                self.assertEqual(response.status_code, 201)
                message = WhatsAppMessage.objects.get(id=response.json()["message"]["id"])
                self.assertEqual(message.message_type, expected_type)
                self.assertEqual(message.account_id, self.account.id)
        self.assertEqual(delay.call_count, 3)
