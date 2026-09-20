import hashlib
import hmac
import json
import time
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from pypdf import PdfReader

from apps.accounts.models import User
from apps.accounts.session_utils import (
    get_session_cookie_name,
    set_authenticated_user,
)
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models import Lead
from apps.organizations.models import Organization
from apps.sales.lifecycle import validate_attachment
from apps.sales.models import (
    DocumentType,
    SalesDocument,
    SalesDocumentDelivery,
    SalesTemplate,
)
from apps.sales.models_lifecycle import (
    SalesPayment,
    SalesPaymentCheckout,
    SalesPaymentGateway,
    SalesScheduledDelivery,
)
from apps.sales.payments import (
    SalesGatewayError,
    apply_stripe_event,
    verify_razorpay_webhook,
    verify_stripe_webhook,
)
from apps.sales.pdf_service import build_pdf_bytes
from apps.sales.services import (
    SalesDeliveryError,
    _delivery_row,
    _sales_template_message,
    deliver_whatsapp,
    render_document_html,
    snapshot_document_presentation,
)
from apps.sales.views import _clean_date


class SalesHardeningBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Sales hardening")
        self.admin = User.objects.create_user(
            email="sales-admin@example.com",
            password="test-password",
            name="Sales Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.agent = User.objects.create_user(
            email="sales-agent@example.com",
            password="test-password",
            name="Sales Agent",
            organization=self.org,
            role=User.Role.AGENT,
        )

    def login(self, user):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

    def document(
        self,
        kind,
        number,
        *,
        status=SalesDocument.Status.DRAFT,
        total="100.00",
    ):
        return SalesDocument.objects.create(
            organization=self.org,
            document_type=kind,
            document_number=number,
            title=number,
            status=status,
            currency="INR",
            issue_date=date(2026, 9, 21),
            subtotal=Decimal(total),
            total=Decimal(total),
            recipient_name="Aarav",
            recipient_email="aarav@example.com",
            recipient_phone="+919999999999",
            rendered_html="<p>Document content</p>",
            presentation_snapshot={
                "accent_color": "#0071e3",
                "header_text": "Sales hardening",
            },
        )


class SalesAttachmentValidationTests(SalesHardeningBase):
    def test_pdf_signature_is_verified(self):
        upload = SimpleUploadedFile(
            "terms.pdf",
            b"%PDF-1.7\nminimal",
            content_type="application/pdf",
        )
        filename, mime_type = validate_attachment(upload)
        self.assertEqual(filename, "terms.pdf")
        self.assertEqual(mime_type, "application/pdf")

    def test_spoofed_pdf_is_rejected(self):
        upload = SimpleUploadedFile(
            "malware.pdf",
            b"MZ-not-a-pdf",
            content_type="application/pdf",
        )
        with self.assertRaises(ValidationError):
            validate_attachment(upload)

    def test_ooxml_requires_zip_signature(self):
        good = SimpleUploadedFile(
            "proposal.docx",
            b"PK\x03\x04" + b"zip-body",
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        self.assertEqual(validate_attachment(good)[0], "proposal.docx")

        bad = SimpleUploadedFile(
            "proposal.docx",
            b"not-a-zip",
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        with self.assertRaises(ValidationError):
            validate_attachment(bad)


class SalesItemTableCustomizationTests(SalesHardeningBase):
    def test_template_can_rename_and_hide_item_columns(self):
        template = SalesTemplate.objects.create(
            organization=self.org,
            document_type=DocumentType.QUOTATION,
            name="Training quote",
            body_template="<section>{{items_table}}</section>",
            item_table_config={
                "show_name": True,
                "show_description": True,
                "description_separate": True,
                "show_qty": True,
                "show_rate": True,
                "show_tax": False,
                "show_amount": True,
                "show_summary": False,
                "labels": {
                    "name": "Course",
                    "description": "Delivery",
                    "qty": "Students",
                    "rate": "Price / student",
                    "amount": "Line total",
                },
            },
        )
        document = self.document(
            DocumentType.QUOTATION,
            "QT-CUSTOM-1",
        )
        document.template = template
        document.line_items = [
            {
                "name": "Security+",
                "description": "Live online",
                "qty": "10.00",
                "rate": "1000.00",
                "tax_rate": "18.00",
                "amount": "10000.00",
            }
        ]
        document.subtotal = Decimal("10000")
        document.tax_total = Decimal("1800")
        document.total = Decimal("11800")
        document.save()

        snapshot_document_presentation(document)
        rendered = render_document_html(document)

        self.assertIn(">Course<", rendered)
        self.assertIn(">Students<", rendered)
        self.assertIn(">Price / student<", rendered)
        self.assertIn(">Delivery<", rendered)
        self.assertNotIn(">Tax<", rendered)
        self.assertNotIn("Subtotal", rendered)


class SalesPDFCertificateTests(SalesHardeningBase):
    def test_signed_agreement_pdf_contains_acceptance_certificate(self):
        agreement = self.document(
            DocumentType.AGREEMENT,
            "AGR-CERT-1",
            status=SalesDocument.Status.SIGNED,
        )
        agreement.accepted_by_name = "Aarav Mehta"
        agreement.accepted_by_email = "aarav@example.com"
        agreement.accepted_ip = "203.0.113.10"
        agreement.signed_at = date(2026, 9, 21)
        agreement.save()

        data = build_pdf_bytes(agreement)
        reader = PdfReader(BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)

        self.assertIn("Electronic signature certificate", text)
        self.assertIn("Aarav Mehta", text)


class SalesPaymentWebhookTests(SalesHardeningBase):
    def test_stripe_event_is_idempotent_and_exact_amount(self):
        invoice = self.document(
            DocumentType.INVOICE,
            "INV-GW-1",
            status=SalesDocument.Status.UNPAID,
            total="100.00",
        )
        gateway = SalesPaymentGateway.objects.create(
            organization=self.org,
            provider=SalesPaymentGateway.Provider.STRIPE,
            is_enabled=True,
        )
        checkout = SalesPaymentCheckout.objects.create(
            organization=self.org,
            invoice=invoice,
            gateway=gateway,
            amount=Decimal("100.00"),
            currency="INR",
            provider_reference="cs_test_1",
        )
        payload = json.dumps(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_test_1",
                        "payment_intent": "pi_test_1",
                        "amount_total": 10000,
                        "currency": "inr",
                        "client_reference_id": str(checkout.id),
                        "metadata": {"checkout_id": str(checkout.id)},
                    }
                },
            }
        ).encode()

        apply_stripe_event(gateway=gateway, body=payload)
        apply_stripe_event(gateway=gateway, body=payload)

        self.assertEqual(
            SalesPayment.objects.filter(
                invoice=invoice,
                external_payment_id="pi_test_1",
            ).count(),
            1,
        )
        checkout.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(checkout.status, SalesPaymentCheckout.Status.PAID)
        self.assertEqual(invoice.status, SalesDocument.Status.PAID)

    def test_stripe_amount_mismatch_is_rejected(self):
        invoice = self.document(
            DocumentType.INVOICE,
            "INV-GW-2",
            status=SalesDocument.Status.UNPAID,
            total="100.00",
        )
        gateway = SalesPaymentGateway.objects.create(
            organization=self.org,
            provider=SalesPaymentGateway.Provider.STRIPE,
            is_enabled=True,
        )
        checkout = SalesPaymentCheckout.objects.create(
            organization=self.org,
            invoice=invoice,
            gateway=gateway,
            amount=Decimal("100.00"),
            currency="INR",
            provider_reference="cs_test_bad",
        )
        payload = json.dumps(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_test_bad",
                        "payment_intent": "pi_test_bad",
                        "amount_total": 9999,
                        "currency": "inr",
                        "client_reference_id": str(checkout.id),
                    }
                },
            }
        ).encode()

        with self.assertRaises(SalesGatewayError):
            apply_stripe_event(gateway=gateway, body=payload)

        self.assertFalse(
            SalesPayment.objects.filter(invoice=invoice).exists()
        )

    def test_gateway_signature_verifiers(self):
        razorpay = SalesPaymentGateway(
            organization=self.org,
            provider=SalesPaymentGateway.Provider.RAZORPAY,
        )
        razorpay.set_webhook_secret("rzp-secret")
        body = b'{"event":"payment_link.paid"}'
        signature = hmac.new(
            b"rzp-secret",
            body,
            hashlib.sha256,
        ).hexdigest()
        self.assertTrue(
            verify_razorpay_webhook(
                gateway=razorpay,
                body=body,
                signature=signature,
            )
        )

        stripe = SalesPaymentGateway(
            organization=self.org,
            provider=SalesPaymentGateway.Provider.STRIPE,
        )
        stripe.set_webhook_secret("whsec_test")
        timestamp = int(time.time())
        signed = f"{timestamp}.".encode() + body
        stripe_signature = hmac.new(
            b"whsec_test",
            signed,
            hashlib.sha256,
        ).hexdigest()
        self.assertTrue(
            verify_stripe_webhook(
                gateway=stripe,
                body=body,
                signature_header=f"t={timestamp},v1={stripe_signature}",
            )
        )


class SalesAutomatedDeliveryIdempotencyTests(SalesHardeningBase):
    def test_schedule_channel_cannot_have_two_delivery_rows(self):
        document = self.document(DocumentType.QUOTATION, "QT-SCHEDULE-1")
        schedule = SalesScheduledDelivery.objects.create(
            organization=self.org,
            document=document,
            channels=["email"],
            scheduled_at=timezone.now(),
        )
        SalesDocumentDelivery.objects.create(
            organization=self.org,
            document=document,
            scheduled_delivery=schedule,
            channel=SalesDocumentDelivery.Channel.EMAIL,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SalesDocumentDelivery.objects.create(
                    organization=self.org,
                    document=document,
                    scheduled_delivery=schedule,
                    channel=SalesDocumentDelivery.Channel.EMAIL,
                )

    def test_ambiguous_queued_automated_email_is_not_recreated(self):
        document = self.document(DocumentType.QUOTATION, "QT-SCHEDULE-2")
        schedule = SalesScheduledDelivery.objects.create(
            organization=self.org,
            document=document,
            channels=["email"],
            scheduled_at=timezone.now(),
        )
        first, created = _delivery_row(
            document=document,
            channel=SalesDocumentDelivery.Channel.EMAIL,
            user=None,
            scheduled_delivery=schedule,
        )
        self.assertTrue(created)

        with self.assertRaises(SalesDeliveryError):
            _delivery_row(
                document=document,
                channel=SalesDocumentDelivery.Channel.EMAIL,
                user=None,
                scheduled_delivery=schedule,
            )

        self.assertEqual(
            SalesDocumentDelivery.objects.filter(
                scheduled_delivery=schedule,
                channel=SalesDocumentDelivery.Channel.EMAIL,
            ).count(),
            1,
        )
        self.assertIsNotNone(first.id)


class SalesMetaDocumentTemplateTests(SalesHardeningBase):
    def test_document_header_template_builds_pdf_header_component(self):
        account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="phone-1",
            display_phone_number="+919999999991",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        template = WhatsAppTemplate.objects.create(
            organization=self.org,
            account=account,
            name="invoice_pdf",
            status=WhatsAppTemplate.Status.APPROVED,
            body="Invoice {{document_number}} total {{document_total}}",
            attachment_type=WhatsAppTemplate.AttachmentType.DOCUMENT,
            meta_template_id="meta-template-1",
            created_by=self.admin,
        )
        WhatsAppTemplateMetadata.objects.create(
            template=template,
            language="en_US",
            placeholder_mapping={
                "1": "document_number",
                "2": "document_total",
            },
        )
        document = self.document(
            DocumentType.INVOICE,
            "INV-META-1",
            status=SalesDocument.Status.UNPAID,
            total="123.00",
        )

        body, payload = _sales_template_message(
            template=template,
            document=document,
            user=self.admin,
            pdf_url="https://example.com/invoice.pdf",
            public_url="https://example.com/invoice",
        )

        self.assertIn("INV-META-1", body)
        self.assertEqual(payload["transport"], "template")
        header = payload["components"][0]
        self.assertEqual(header["type"], "header")
        self.assertEqual(
            header["parameters"][0]["document"]["link"],
            "https://example.com/invoice.pdf",
        )

    @patch("apps.sales.pdf_service.ensure_document_pdf")
    @patch(
        "services.channels.whatsapp_api_chat_service."
        "is_within_api_24h_window",
        return_value=False,
    )
    def test_wrong_account_template_is_rejected(
        self,
        _window,
        _pdf,
    ):
        pipeline = self.org.pipelines.get(name="Leads")
        pipeline.phone_number = "+919999999992"
        pipeline.save(update_fields=["phone_number"])
        stage = pipeline.stages.order_by("display_order").first()
        lead = Lead.objects.create(
            organization=self.org,
            pipeline=pipeline,
            stage=stage,
            name="Template lead",
            phone="+919888888888",
        )
        correct = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="correct-id",
            display_phone_number="+919999999992",
            access_token="token-correct",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        wrong = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="wrong-id",
            display_phone_number="+919999999993",
            access_token="token-wrong",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        wrong_template = WhatsAppTemplate.objects.create(
            organization=self.org,
            account=wrong,
            name="wrong_pdf",
            status=WhatsAppTemplate.Status.APPROVED,
            body="Your document",
            attachment_type=WhatsAppTemplate.AttachmentType.DOCUMENT,
            meta_template_id="meta-wrong",
            created_by=self.admin,
        )
        document = self.document(
            DocumentType.QUOTATION,
            "QT-META-WRONG",
        )
        document.lead = lead
        document.save(update_fields=["lead"])

        with self.assertRaises(SalesDeliveryError):
            deliver_whatsapp(
                document=document,
                user=self.admin,
                body="Quotation",
                base_url="https://example.com/",
                attach_pdf=True,
                whatsapp_template_id=str(wrong_template.id),
            )

        document.refresh_from_db()
        self.assertEqual(document.status, SalesDocument.Status.DRAFT)
        self.assertEqual(correct.organization_id, self.org.id)


class SalesAdminPermissionTests(SalesHardeningBase):
    def test_agent_cannot_open_sales_settings_or_templates(self):
        self.login(self.agent)
        self.assertEqual(
            self.client.get(reverse("shvya-sales-settings")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("shvya-sales-template-list")).status_code,
            403,
        )

    def test_agent_cannot_record_invoice_payment(self):
        invoice = self.document(
            DocumentType.INVOICE,
            "INV-PERM-1",
            status=SalesDocument.Status.UNPAID,
        )
        self.login(self.agent)

        response = self.client.post(
            reverse("shvya-sales-manual-payment", args=[invoice.id]),
            {"amount": "10.00", "method": "upi"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(invoice.payments.exists())

    def test_admin_can_open_sales_settings(self):
        self.login(self.admin)
        response = self.client.get(reverse("shvya-sales-settings"))
        self.assertEqual(response.status_code, 200)


class SalesInputValidationTests(SalesHardeningBase):
    def test_invalid_calendar_date_is_rejected(self):
        with self.assertRaises(ValidationError):
            _clean_date(
                "2026-02-31",
                label="Issue date",
                required=True,
            )
