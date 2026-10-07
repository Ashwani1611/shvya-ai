"""Touchpoint authoring, tenant safety and send-time personalization regression tests."""

from unittest.mock import MagicMock, patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import JsonResponse
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.followups.models import TouchpointAttachment, TouchpointCategory, TouchpointReply
from apps.organizations.models import Organization
from services.channels.whatsapp_service import _send_outbound_media_message
from services.touchpoint_service import render_touchpoint


class TouchpointPersonalizationFilesTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(package="dfy", name="Touchpoint test")
        self.other = Organization.objects.create(package="dfy", name="Another business")
        self.user = User.objects.create_user(
            email="touchpoint-files@example.com", name="Operator",
            organization=self.org, role=User.Role.ADMIN, password="test",
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key
        self.pipeline = Pipeline.objects.create(
            organization=self.org, name="Inbound", phone_number="tp-linked",
        )
        self.stage = Stage.objects.create(pipeline=self.pipeline, name="New")
        self.lead = Lead.objects.create(
            organization=self.org, pipeline=self.pipeline, stage=self.stage,
            name="Priya Sharma", phone="+919876543210", lead_source="whatsapp_api",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org, phone_number_id="tp-linked",
            connection_type="api", status="connected", is_active=True,
        )
        self.category = TouchpointCategory.objects.create(
            organization=self.org, name="Sales",
        )
        self.manage = reverse("crm-auto-follow-ups-touchpoints")
        self.panel = reverse("chat-contact-panel", args=[self.lead.pk])
        self.created_paths = []

    def tearDown(self):
        # The suite rolls back its database transaction; remove test media now.
        from apps.support.storage import private_storage
        for path in self.created_paths:
            try:
                private_storage.delete(path)
            except (FileNotFoundError, OSError):
                pass
        super().tearDown()

    def make_touchpoint(self, *, body="Hello {{lead_first_name}}", filename="guide.pdf"):
        upload = SimpleUploadedFile(
            filename, b"%PDF-1.4\nTouchpoint sample\n%%EOF", content_type="application/pdf",
        )
        response = self.client.post(self.manage, {
            "action": "save_reply", "category_id": str(self.category.pk),
            "title": "Welcome", "body": body, "attachments": upload,
        })
        self.assertEqual(response.status_code, 200, response.content)
        reply = TouchpointReply.objects.get(category=self.category)
        item = reply.attachments.get()
        self.created_paths.append(item.file.name)
        return reply, item

    def test_configured_attribute_offered_before_lead_has_a_value(self):
        AttributeDefinition.objects.create(
            organization=self.org, name="Product Interest", key="product_interest",
        )
        response = self.client.get(self.manage)
        self.assertContains(response, 'data-followup-placeholder="{{product_interest}}"')
        reply, item = self.make_touchpoint(
            body="Hi {{lead_first_name}}, your interest: {{product_interest}}",
        )
        preview = self.client.get(self.panel, {"channel": "whatsapp"})
        self.assertContains(preview, "Hi Priya, your interest:")
        self.assertContains(preview, "Missing CRM values: product_interest")
        self.assertNotContains(preview, "Hi {{lead_first_name}}")
        self.lead.attributes = {"product_interest": "Cloud Training"}
        self.lead.save(update_fields=["attributes"])
        updated = self.client.get(self.panel, {"channel": "whatsapp"})
        self.assertContains(updated, "Hi Priya, your interest: Cloud Training")
        self.assertNotContains(updated, "Missing CRM values: product_interest")
        self.assertEqual(
            render_touchpoint(reply=reply, lead=self.lead, user=self.user),
            ("Hi Priya, your interest: Cloud Training", []),
        )

    def test_create_edit_remove_attachments_without_losing_unselected_files(self):
        reply, attachment = self.make_touchpoint()
        self.assertContains(self.client.get(self.manage), "guide.pdf")
        response = self.client.post(self.manage, {
            "action": "save_reply", "reply_id": str(reply.pk),
            "category_id": str(self.category.pk), "title": "Updated", "body": "Thanks",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(reply.attachments.count(), 1)
        self.assertEqual(reply.attachments.get().pk, attachment.pk)
        response = self.client.post(self.manage, {
            "action": "save_reply", "reply_id": str(reply.pk),
            "category_id": str(self.category.pk), "title": "Updated", "body": "Thanks",
            "remove_attachments": [str(attachment.pk)],
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(reply.attachments.exists())

    def test_download_requires_matching_tenant(self):
        _, attachment = self.make_touchpoint()
        url = reverse("crm-touchpoint-attachment-download", args=[attachment.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"].lower())
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4\nTouchpoint sample\n%%EOF")
        attachment.reply.category.organization = self.other
        attachment.reply.category.save(update_fields=["organization"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_rejects_unknown_and_unowned_attachment_mutations(self):
        invalid = self.client.post(self.manage, {
            "action": "save_reply", "category_id": str(self.category.pk),
            "title": "Wrong", "body": "Hello {{other_private_value}}",
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("Unknown CRM placeholder", invalid.json()["error"])
        reply, attachment = self.make_touchpoint()
        new_file = SimpleUploadedFile("bad.exe", b"malware", content_type="application/octet-stream")
        bad = self.client.post(self.manage, {
            "action": "save_reply", "reply_id": str(reply.pk),
            "category_id": str(self.category.pk),
            "title": "Bad file", "body": "Safe",
            "attachments": new_file,
        })
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(reply.attachments.count(), 1)
        invalid_remove = self.client.post(self.manage, {
            "action": "save_reply", "reply_id": str(reply.pk),
            "category_id": str(self.category.pk),
            "title": "Bad remove", "body": "Safe",
            "remove_attachments": ["ff000000-1111-4222-8333-000000000001"],
        })
        self.assertEqual(invalid_remove.status_code, 400)
        reply.refresh_from_db()
        self.assertEqual(reply.title, "Welcome")

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    @patch("services.channels.whatsapp_api_chat_service.is_within_api_24h_window", return_value=True)
    def test_api_send_queues_private_file_reference_and_uploads_at_delivery(self, window, delay):
        _, attachment = self.make_touchpoint()
        url = reverse("chat-touchpoint-attachment-send", args=[self.lead.pk, attachment.pk])
        result = self.client.post(url, {
            "channel": "whatsapp", "account": str(self.account.pk),
        })
        self.assertEqual(result.status_code, 202, result.content)
        message = WhatsAppMessage.objects.get(
            organization=self.org, direction=WhatsAppMessage.Direction.OUTBOUND,
        )
        self.assertEqual(message.message_type, WhatsAppMessage.MessageType.DOCUMENT)
        self.assertEqual(message.media_payload, {
            "source": "touchpoint", "attachment_id": str(attachment.pk),
        })
        delay.assert_called_once_with(str(message.id))
        provider = MagicMock()
        provider.upload_media.return_value = {"id": "meta-file-123"}
        _send_outbound_media_message(client=provider, message=message)
        self.assertEqual(provider.upload_media.call_args.kwargs["filename"], "guide.pdf")
        self.assertEqual(provider.send_media_message.call_args.kwargs["media_id"], "meta-file-123")

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    @patch("services.channels.whatsapp_api_chat_service.is_within_api_24h_window", return_value=False)
    def test_closed_api_window_does_not_queue_file(self, window, delay):
        _, attachment = self.make_touchpoint()
        url = reverse("chat-touchpoint-attachment-send", args=[self.lead.pk, attachment.pk])
        result = self.client.post(url, {
            "channel": "whatsapp", "account": str(self.account.pk),
        })
        self.assertEqual(result.status_code, 400)
        self.assertIn("24-hour", result.json()["error"])
        delay.assert_not_called()

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    def test_cross_tenant_and_other_sender_cannot_send_saved_files(self, delay):
        _, attachment = self.make_touchpoint()
        url = reverse("chat-touchpoint-attachment-send", args=[self.lead.pk, attachment.pk])
        result = self.client.post(url, {"channel": "whatsapp", "account": "forged"})
        self.assertEqual(result.status_code, 400)
        attachment.reply.category.organization = self.other
        attachment.reply.category.save(update_fields=["organization"])
        result = self.client.post(url, {
            "channel": "whatsapp", "account": str(self.account.pk),
        })
        self.assertEqual(result.status_code, 404)
        delay.assert_not_called()

    @patch("apps.channels.hosted_send_ui._send_manual_response")
    @patch("services.channels.hosted_send_service.queue_hosted_uploaded_media")
    def test_hosted_uses_existing_media_transport_not_meta(self, queue, send):
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type"])
        _, attachment = self.make_touchpoint()
        url = reverse("chat-touchpoint-attachment-send", args=[self.lead.pk, attachment.pk])
        send.return_value = JsonResponse({"ok": True, "status": "sent"})
        result = self.client.post(url, {
            "channel": "hosted", "account": str(self.account.pk),
        })
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(queue.call_args.kwargs["message_type"], WhatsAppMessage.MessageType.DOCUMENT)
        self.assertEqual(queue.call_args.kwargs["lead"].id, self.lead.id)
        send.assert_called_once()

    def test_instagram_shows_download_but_no_unsupported_file_send(self):
        _, attachment = self.make_touchpoint()
        response = self.client.get(self.panel, {"channel": "instagram"})
        self.assertContains(response, reverse("crm-touchpoint-attachment-download", args=[attachment.pk]))
        self.assertNotContains(response, "data-send-touchpoint-file")
        self.assertContains(response, "Instagram currently supports Touchpoint text")
