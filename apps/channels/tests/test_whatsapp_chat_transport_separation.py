import tempfile
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class WhatsAppChatTransportSeparationTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()

        self.org = Organization.objects.create(
            name="Transport Separation Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="transport@example.com",
            password="test-password",
            name="Transport Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Sales",
            country_code="+91",
            phone_number="9000000000",
            owner=self.user,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New",
            display_order=1,
        )
        self.api_account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="meta-phone-id",
            display_phone_number="+919000000000",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.hosted_account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type="hosted",
            phone_number_id="+919000000000",
            display_phone_number="+919000000000",
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

    def make_lead(self, name, phone):
        return Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name=name,
            phone=phone,
        )

    def make_message(self, *, account, lead, body, direction="inbound"):
        return WhatsAppMessage.objects.create(
            organization=self.org,
            account=account,
            lead=lead,
            direction=direction,
            from_number=lead.phone if direction == "inbound" else account.display_phone_number,
            to_number=account.display_phone_number if direction == "inbound" else lead.phone,
            body=body,
            status=(
                WhatsAppMessage.Status.RECEIVED
                if direction == "inbound"
                else WhatsAppMessage.Status.SENT
            ),
            is_read=False if direction == "inbound" else True,
        )

    def test_api_inbox_excludes_hosted_only_conversation(self):
        api_lead = self.make_lead("API Lead", "+919111111111")
        hosted_lead = self.make_lead("Hosted Lead", "+919222222222")
        self.make_message(account=self.api_account, lead=api_lead, body="API only")
        self.make_message(account=self.hosted_account, lead=hosted_lead, body="Hosted only")

        response = self.client.get(reverse("whatsapp-chats"))

        self.assertEqual(response.status_code, 200)
        ids = {lead.id for lead in response.context["conversations"]}
        self.assertIn(api_lead.id, ids)
        self.assertNotIn(hosted_lead.id, ids)
        self.assertContains(response, "API only")
        self.assertNotContains(response, "Hosted only")
        self.assertEqual(
            list(response.context["accounts"].values_list("id", flat=True)),
            [self.api_account.id],
        )

    def test_api_thread_excludes_hosted_message_for_same_lead(self):
        lead = self.make_lead("Mixed Lead", "+919333333333")
        self.make_message(account=self.api_account, lead=lead, body="Visible API message")
        self.make_message(account=self.hosted_account, lead=lead, body="Hidden Hosted message")

        response = self.client.get(reverse("whatsapp-chat-detail", args=[lead.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visible API message")
        self.assertNotContains(response, "Hidden Hosted message")
        self.assertEqual(len(response.context["chat_messages"]), 1)
        self.assertEqual(response.context["chat_messages"][0].account_id, self.api_account.id)

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    def test_api_send_never_selects_newer_hosted_account(self, delay):
        lead = self.make_lead("Send Lead", "+919444444444")
        self.make_message(account=self.api_account, lead=lead, body="Fresh API inbound")
        self.make_message(account=self.hosted_account, lead=lead, body="Newer Hosted")

        response = self.client.post(
            reverse("whatsapp-send-message", args=[lead.id]),
            {"body": "API reply"},
        )

        self.assertEqual(response.status_code, 202)
        queued = WhatsAppMessage.objects.get(id=response.json()["id"])
        self.assertEqual(queued.account_id, self.api_account.id)
        self.assertEqual(queued.account.connection_type, WhatsAppAccount.ConnectionType.API)
        delay.assert_called_once_with(str(queued.id))

    @patch("apps.channels.hosted_send_ui.send_hosted_whatsapp_message_task.delay")
    def test_hosted_photo_upload_queues_hosted_media_task(self, delay):
        lead = self.make_lead("Hosted Media Lead", "+919555555555")
        upload = SimpleUploadedFile(
            "photo.jpg",
            b"\xff\xd8\xff\xe0fake-jpeg-data",
            content_type="image/jpeg",
        )

        response = self.client.post(
            reverse("whatsapp-hosted-session-chat-media-send", args=[self.hosted_account.id]),
            {
                "chat": lead.phone,
                "message_type": "image",
                "caption": "Photo caption",
                "attachment": upload,
            },
        )

        self.assertEqual(response.status_code, 201)
        message = WhatsAppMessage.objects.get(id=response.json()["message"]["id"])
        self.assertEqual(message.account_id, self.hosted_account.id)
        self.assertEqual(message.message_type, WhatsAppMessage.MessageType.IMAGE)
        self.assertEqual(message.body, "Photo caption")
        self.assertEqual(message.media_payload["source"], "storage")
        self.assertTrue(message.media_payload["storage_path"])
        delay.assert_called_once_with(str(message.id))

    def test_hosted_chat_page_exposes_attachment_picker(self):
        lead = self.make_lead("Hosted UI Lead", "+919666666666")
        self.make_message(account=self.hosted_account, lead=lead, body="Hosted chat")

        response = self.client.get(
            reverse("whatsapp-hosted-session-chats", args=[self.hosted_account.id]),
            {"chat": lead.phone},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Attach photo, video or document")
        self.assertContains(response, "data-kind=\"image\"")
        self.assertContains(response, "data-kind=\"video\"")
        self.assertContains(response, "data-kind=\"document\"")
