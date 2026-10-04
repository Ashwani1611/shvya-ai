from datetime import date
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import (
    get_session_cookie_name,
    set_authenticated_user,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead
from apps.organizations.models import Organization
from apps.sales.models import (
    DocumentType,
    SalesDocument,
    SalesDocumentDelivery,
    SalesTemplate,
)
from apps.sales.services import deliver_whatsapp


class SalesHotfixBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(package="enterprise", name="Sales hotfix org")
        self.admin = User.objects.create_user(
            email="sales-hotfix-admin@example.com",
            password="test-password",
            name="Sales Hotfix Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )

    def login(self):
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

    def template_payload(self, *, name="Custom quotation"):
        return {
            "document_type": DocumentType.QUOTATION,
            "name": name,
            "number_prefix": "QT",
            "accent_color": "#0071e3",
            "header_text": "Sales hotfix org",
            "body_template": "<section><h2>{{document.title}}</h2>{{items_table}}</section>",
            "footer_text": "Thank you.",
            "email_subject_template": "Quotation {{document.number}}",
            "email_body_template": "Hello {{recipient.name}}",
            "whatsapp_body_template": "Quotation {{document.number}}",
            "item_show_name": "on",
            "item_show_qty": "on",
            "item_show_rate": "on",
            "item_show_amount": "on",
            "item_show_summary": "on",
            "is_active": "on",
        }


class SalesTemplateSaveHotfixTests(SalesHotfixBase):
    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    })
    def test_edit_saves_logo_and_signature_with_mismatched_extensions(self):
        template = SalesTemplate.objects.create(
            organization=self.org,
            document_type=DocumentType.QUOTATION,
            name="Branding quotation",
            body_template="<section>{{document.title}}</section>",
            created_by=self.admin,
        )
        self.login()
        payload = self.template_payload(name="Updated branding quotation")
        contents = {}
        for field, name, image_format in (
            ("logo_file", "logo.jpg", "PNG"),
            ("signature_file", "signature.png", "JPEG"),
        ):
            content = BytesIO()
            Image.new("RGB", (8, 8), "white").save(content, format=image_format)
            contents[field] = content.getvalue()
            payload[field] = SimpleUploadedFile(
                name, contents[field], content_type="application/octet-stream",
            )
        response = self.client.post(
            reverse("shvya-sales-template-edit", args=[template.id]), payload,
        )
        self.assertEqual(response.status_code, 302)
        template.refresh_from_db()
        self.assertTrue(template.logo_file.name.endswith(".png"))
        self.assertTrue(template.signature_file.name.endswith(".jpg"))
        for field, content in contents.items():
            with getattr(template, field).open("rb") as stored:
                self.assertEqual(stored.read(), content)

    def test_plain_validation_error_is_rendered_instead_of_500(self):
        self.login()

        with patch(
            "apps.sales.views.validate_brand_asset",
            side_effect=ValidationError("Logo image is invalid."),
        ):
            response = self.client.post(
                reverse("shvya-sales-template-create"),
                self.template_payload(),
            )

        self.assertEqual(response.status_code, 200)
        rendered_messages = [str(message) for message in response.context["messages"]]
        self.assertIn("Logo image is invalid.", rendered_messages)

    def test_new_template_saves_successfully(self):
        self.login()

        response = self.client.post(
            reverse("shvya-sales-template-create"),
            self.template_payload(),
        )

        self.assertEqual(response.status_code, 302)
        template = SalesTemplate.objects.get(
            organization=self.org,
            document_type=DocumentType.QUOTATION,
            name="Custom quotation",
        )
        self.assertTrue(template.is_active)
        self.assertEqual(template.item_table_config["labels"]["name"], "Item")

    def test_existing_template_saves_successfully(self):
        template = SalesTemplate.objects.create(
            organization=self.org,
            document_type=DocumentType.QUOTATION,
            name="Original quotation",
            body_template="<section>{{document.title}}</section>",
            created_by=self.admin,
        )
        self.login()

        response = self.client.post(
            reverse("shvya-sales-template-edit", args=[template.id]),
            self.template_payload(name="Updated quotation"),
        )

        self.assertEqual(response.status_code, 302)
        template.refresh_from_db()
        self.assertEqual(template.name, "Updated quotation")
        self.assertEqual(template.number_prefix, "QT")


class SalesHostedPdfHotfixTests(SalesHotfixBase):
    def setUp(self):
        super().setUp()
        pipeline = self.org.pipelines.get(name="Leads")
        pipeline.phone_number = "+919999999991"
        pipeline.save(update_fields=["phone_number"])
        stage = pipeline.stages.order_by("display_order").first()
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=pipeline,
            stage=stage,
            name="Hosted PDF lead",
            phone="+919888888888",
            email="hosted-pdf@example.com",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            phone_number_id="+919999999991",
            display_phone_number="+919999999991",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.document = SalesDocument.objects.create(
            organization=self.org,
            document_type=DocumentType.QUOTATION,
            lead=self.lead,
            created_by=self.admin,
            document_number="QT-HOSTED-001",
            title="Hosted PDF quotation",
            status=SalesDocument.Status.DRAFT,
            recipient_name=self.lead.name,
            recipient_email=self.lead.email,
            recipient_phone=self.lead.phone,
            currency="INR",
            issue_date=date(2026, 9, 21),
            subtotal="100.00",
            total="100.00",
            rendered_html="<p>Quotation</p>",
            presentation_snapshot={"header_text": "Sales hotfix org"},
        )

    def test_hosted_pdf_uses_private_uploaded_media_worker(self):
        pdf_bytes = b"%PDF-1.7\nSHVYA sales hosted PDF\n%%EOF"

        def fake_hosted_queue(
            *,
            account,
            to_number,
            uploaded_file,
            message_type,
            caption="",
            lead=None,
        ):
            self.assertEqual(account.id, self.account.id)
            self.assertEqual(to_number, self.lead.phone)
            self.assertEqual(message_type, WhatsAppMessage.MessageType.DOCUMENT)
            self.assertEqual(uploaded_file.name, "QT-HOSTED-001.pdf")
            self.assertEqual(uploaded_file.content_type, "application/pdf")
            self.assertEqual(uploaded_file.read(), pdf_bytes)
            return WhatsAppMessage.objects.create(
                organization=self.org,
                account=account,
                lead=lead,
                direction=WhatsAppMessage.Direction.OUTBOUND,
                from_number=account.display_phone_number,
                to_number=to_number,
                body=caption,
                message_type=message_type,
                media_payload={
                    "source": "storage",
                    "storage_path": "hosted_whatsapp/outbound/test.pdf",
                    "filename": uploaded_file.name,
                    "mime_type": uploaded_file.content_type,
                },
                status=WhatsAppMessage.Status.QUEUED,
                raw_payload={
                    "shvya_hosted": {
                        "origin": "agent",
                        "chat_id": to_number,
                        "attachment": True,
                    }
                },
            )

        with (
            patch(
                "apps.sales.pdf_service.read_document_pdf",
                return_value=pdf_bytes,
            ) as read_pdf,
            patch(
                "services.channels.hosted_send_service.queue_hosted_uploaded_media",
                side_effect=fake_hosted_queue,
            ) as hosted_queue,
            patch(
                "apps.channels.hosted_send_tasks.send_hosted_whatsapp_message_task.delay"
            ) as hosted_delay,
            patch(
                "services.channels.whatsapp_service.queue_outbound_message"
            ) as generic_queue,
            patch(
                "apps.channels.tasks.send_whatsapp_message_task.delay"
            ) as generic_delay,
        ):
            delivery = deliver_whatsapp(
                document=self.document,
                user=self.admin,
                body="Please review your quotation.",
                base_url="https://dashboard.shvya-ai.com/",
                attach_pdf=True,
            )

        self.assertEqual(delivery.status, SalesDocumentDelivery.Status.QUEUED)
        read_pdf.assert_called_once()
        hosted_queue.assert_called_once()
        hosted_delay.assert_called_once_with(delivery.provider_message_id)
        generic_queue.assert_not_called()
        generic_delay.assert_not_called()

        message = WhatsAppMessage.objects.get(id=delivery.provider_message_id)
        self.assertEqual(message.media_payload["source"], "storage")
        self.assertEqual(
            message.raw_payload["shvya_sales"]["document_number"],
            "QT-HOSTED-001",
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, SalesDocument.Status.SENT)
