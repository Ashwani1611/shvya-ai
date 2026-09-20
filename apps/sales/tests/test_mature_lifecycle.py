from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.organizations.models import Organization
from apps.sales.lifecycle import (
    create_agreement_revision,
    create_credit_note,
    create_recurring_invoice_instance,
    invoice_ledger,
    record_payment,
    record_refund,
)
from apps.sales.models import DocumentType, SalesDocument, SalesDocumentDelivery
from apps.sales.models_lifecycle import (
    SalesRecurringInvoice,
    SalesScheduledDelivery,
    SalesSettings,
)
from apps.sales.pdf_service import build_pdf_bytes, ensure_document_pdf
from apps.sales.tasks import (
    dispatch_scheduled_deliveries_task,
    maintain_sales_documents_task,
)
from apps.sales.tracking import (
    build_tracked_email_html,
    record_email_click,
    record_email_open,
)


class SalesLifecycleTestCase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Sales Lifecycle Org")

    def document(
        self,
        document_type,
        number,
        *,
        status="draft",
        total="100.00",
        due_date=None,
        valid_until=None,
    ):
        return SalesDocument.objects.create(
            organization=self.org,
            document_type=document_type,
            document_number=number,
            title=number,
            status=status,
            issue_date=timezone.localdate(),
            due_date=due_date,
            valid_until=valid_until,
            total=Decimal(total),
            subtotal=Decimal(total),
            recipient_name="Aarav",
            recipient_email="aarav@example.com",
            recipient_phone="+919999999999",
            rendered_html="<h1>Test document</h1><p>Safe content.</p>",
            presentation_snapshot={
                "accent_color": "#0071e3",
                "header_text": "Lifecycle Org",
                "footer_text": "Footer",
            },
        )


class SalesPDFTests(SalesLifecycleTestCase):
    def test_pdf_generation_produces_real_pdf_and_hash(self):
        doc = self.document(DocumentType.QUOTATION, "QT-00100")

        data = build_pdf_bytes(doc)

        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 500)

        ensure_document_pdf(doc)
        doc.refresh_from_db()
        self.assertTrue(doc.pdf_file.name.endswith(".pdf"))
        self.assertEqual(len(doc.pdf_sha256), 64)
        self.assertIsNotNone(doc.pdf_generated_at)

    def test_existing_pdf_is_reused_without_force(self):
        doc = self.document(DocumentType.INVOICE, "INV-00100")
        ensure_document_pdf(doc)
        first_name = doc.pdf_file.name
        first_hash = doc.pdf_sha256

        ensure_document_pdf(doc)
        doc.refresh_from_db()

        self.assertEqual(doc.pdf_file.name, first_name)
        self.assertEqual(doc.pdf_sha256, first_hash)


class SalesEmailTrackingTests(SalesLifecycleTestCase):
    def test_open_and_click_tracking_are_counted(self):
        doc = self.document(
            DocumentType.QUOTATION,
            "QT-00101",
            status=SalesDocument.Status.SENT,
        )
        delivery = SalesDocumentDelivery.objects.create(
            organization=self.org,
            document=doc,
            channel=SalesDocumentDelivery.Channel.EMAIL,
            status=SalesDocumentDelivery.Status.SENT,
            body="Open https://example.com/quote",
            to_identity="aarav@example.com",
        )

        html = build_tracked_email_html(
            delivery=delivery,
            body=delivery.body,
            base_url="https://dashboard.example.com/",
        )
        link = delivery.tracked_links.get()

        self.assertIn("track/open", html)
        self.assertIn("track/click", html)
        self.assertNotIn('href="https://example.com/quote"', html)

        record_email_open(tracking_token=delivery.tracking_token)
        record_email_open(tracking_token=delivery.tracking_token)
        record_email_click(token=link.token)

        delivery.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(delivery.open_count, 2)
        self.assertEqual(delivery.click_count, 1)
        self.assertIsNotNone(delivery.opened_at)
        self.assertIsNotNone(delivery.first_clicked_at)
        self.assertEqual(link.click_count, 1)


class SalesInvoiceLedgerTests(SalesLifecycleTestCase):
    def test_partial_and_full_payments_drive_invoice_status(self):
        invoice = self.document(
            DocumentType.INVOICE,
            "INV-00101",
            status=SalesDocument.Status.UNPAID,
            total="100.00",
        )
        record_payment(
            invoice=invoice,
            amount="25.00",
            method="upi",
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesDocument.Status.PARTIAL)
        self.assertEqual(invoice_ledger(invoice)["balance"], Decimal("75.00"))

        record_payment(
            invoice=invoice,
            amount="75.00",
            method="bank_transfer",
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesDocument.Status.PAID)
        self.assertEqual(invoice_ledger(invoice)["balance"], Decimal("0"))

    def test_refund_reopens_balance_and_credit_can_settle_it(self):
        invoice = self.document(
            DocumentType.INVOICE,
            "INV-00102",
            status=SalesDocument.Status.UNPAID,
            total="100.00",
        )
        record_payment(invoice=invoice, amount="100.00", method="card")
        record_refund(invoice=invoice, amount="20.00")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesDocument.Status.PARTIAL)
        self.assertEqual(invoice_ledger(invoice)["balance"], Decimal("20.00"))

        create_credit_note(invoice=invoice, amount="20.00", apply=True)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesDocument.Status.PAID)
        self.assertEqual(invoice_ledger(invoice)["balance"], Decimal("0"))


class SalesAgreementRevisionTests(SalesLifecycleTestCase):
    def test_signed_agreement_renewal_creates_new_number_and_version(self):
        agreement = self.document(
            DocumentType.AGREEMENT,
            "AGR-00010",
            status=SalesDocument.Status.SIGNED,
        )

        renewed = create_agreement_revision(
            agreement=agreement,
            kind="renewal",
        )

        agreement.refresh_from_db()
        self.assertFalse(agreement.is_current_version)
        self.assertTrue(renewed.is_current_version)
        self.assertEqual(renewed.revision_number, 2)
        self.assertEqual(renewed.revision_kind, "renewal")
        self.assertEqual(renewed.supersedes_id, agreement.id)
        self.assertNotEqual(renewed.document_number, agreement.document_number)
        self.assertEqual(renewed.status, SalesDocument.Status.DRAFT)


class SalesRecurringInvoiceTests(SalesLifecycleTestCase):
    def test_due_rule_creates_new_invoice_and_advances_schedule(self):
        source = self.document(
            DocumentType.INVOICE,
            "INV-00110",
            status=SalesDocument.Status.PAID,
            total="250.00",
        )
        source.due_date = source.issue_date + timedelta(days=10)
        source.save(update_fields=["due_date"])
        due = timezone.now() - timedelta(minutes=1)
        rule = SalesRecurringInvoice.objects.create(
            organization=self.org,
            source_invoice=source,
            interval_count=1,
            interval_unit=SalesRecurringInvoice.IntervalUnit.MONTH,
            next_run_at=due,
            max_cycles=2,
        )

        generated = create_recurring_invoice_instance(rule)

        self.assertIsNotNone(generated)
        self.assertEqual(generated.document_type, DocumentType.INVOICE)
        self.assertEqual(generated.total, Decimal("250.00"))
        self.assertEqual(generated.source_document_id, source.id)
        rule.refresh_from_db()
        self.assertEqual(rule.cycles_created, 1)
        self.assertGreater(rule.next_run_at, due)


class SalesMaintenanceTests(SalesLifecycleTestCase):
    def test_expired_quotation_is_closed_without_sending_when_reminders_disabled(self):
        SalesSettings.objects.create(
            organization=self.org,
            automatic_email_reminders=False,
        )
        quote = self.document(
            DocumentType.QUOTATION,
            "QT-00120",
            status=SalesDocument.Status.SENT,
            valid_until=timezone.localdate() - timedelta(days=1),
        )

        maintain_sales_documents_task.run()

        quote.refresh_from_db()
        self.assertEqual(quote.status, SalesDocument.Status.EXPIRED)
        self.assertTrue(
            quote.activities.filter(event_type="quotation_expired").exists()
        )

    @patch("apps.sales.tasks.deliver_email")
    def test_due_scheduled_email_is_claimed_once(self, deliver_email):
        doc = self.document(
            DocumentType.QUOTATION,
            "QT-00121",
            status=SalesDocument.Status.DRAFT,
        )
        schedule = SalesScheduledDelivery.objects.create(
            organization=self.org,
            document=doc,
            channels=["email"],
            email_subject="Subject",
            email_body="Body",
            scheduled_at=timezone.now() - timedelta(seconds=1),
            base_url="https://dashboard.example.com/",
        )

        dispatch_scheduled_deliveries_task.run()
        schedule.refresh_from_db()

        self.assertEqual(
            schedule.status,
            SalesScheduledDelivery.Status.COMPLETED,
        )
        deliver_email.assert_called_once()
