from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels import whatsapp_service
from services.channels.whatsapp_template_delivery import (
    WhatsAppTemplateSendError,
    queue_template_message,
)


class WhatsAppTemplateDeliveryTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Template Delivery Org")
        self.user = User.objects.create_user(
            email="template-delivery@example.com",
            password="test-password",
            name="Template Agent",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            business_name="Template Sender",
            phone_number_id="123456789",
            waba_id="987654321",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage = Stage.objects.create(pipeline=self.pipeline, name="New")
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Jane Customer",
            phone="+919000000001",
            email="jane@example.com",
        )
        self.template = WhatsAppTemplate.objects.create(
            organization=self.org,
            account=self.account,
            created_by=self.user,
            name="approved_welcome",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.APPROVED,
            body="Hi {{lead_first_name}}, welcome to {{org_name}}",
            meta_template_id="meta-template-1",
        )
        WhatsAppTemplateMetadata.objects.create(
            template=self.template,
            local_status=WhatsAppTemplateMetadata.LocalStatus.SYNCED,
            language="en_US",
            placeholder_mapping={"1": "lead_first_name", "2": "org_name"},
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def test_queue_template_uses_template_account_and_real_transport_metadata(self):
        message = queue_template_message(
            template=self.template,
            lead=self.lead,
            user=self.user,
        )

        self.assertEqual(message.account_id, self.template.account_id)
        self.assertEqual(message.status, WhatsAppMessage.Status.QUEUED)
        self.assertEqual(message.media_payload["transport"], "template")
        self.assertEqual(message.media_payload["template_name"], "approved_welcome")
        self.assertEqual(message.media_payload["language_code"], "en_US")
        self.assertEqual(
            message.media_payload["components"],
            [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Jane"},
                        {"type": "text", "text": "Template Delivery Org"},
                    ],
                }
            ],
        )
        self.assertEqual(message.body, "Hi Jane, welcome to Template Delivery Org")

    @patch("services.channels.whatsapp_service.WhatsAppClient.send_template_message")
    def test_worker_transport_calls_meta_template_api(self, send_template):
        send_template.return_value = {
            "messaging_product": "whatsapp",
            "messages": [{"id": "wamid.template.success"}],
        }
        message = queue_template_message(
            template=self.template,
            lead=self.lead,
            user=self.user,
        )

        whatsapp_service.send_outbound_message(message=message)

        send_template.assert_called_once_with(
            to=self.lead.phone,
            template_name="approved_welcome",
            language_code="en_US",
            components=[
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Jane"},
                        {"type": "text", "text": "Template Delivery Org"},
                    ],
                }
            ],
        )
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)
        self.assertEqual(message.external_id, "wamid.template.success")

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    def test_chat_template_endpoint_queues_real_template_transport(self, delay):
        response = self.client.post(
            reverse("whatsapp-send-template", args=[self.lead.pk]),
            {"template_id": str(self.template.id)},
        )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["transport"], "template")
        message = WhatsAppMessage.objects.get(id=data["id"])
        self.assertEqual(message.media_payload["transport"], "template")
        self.assertEqual(message.media_payload["template_name"], self.template.name)
        delay.assert_called_once_with(str(message.id))

    def test_media_header_template_is_rejected_before_queue(self):
        self.template.attachment_type = WhatsAppTemplate.AttachmentType.IMAGE
        self.template.save(update_fields=["attachment_type", "updated_at"])

        with self.assertRaisesRegex(WhatsAppTemplateSendError, "requires a media header"):
            queue_template_message(
                template=self.template,
                lead=self.lead,
                user=self.user,
            )
