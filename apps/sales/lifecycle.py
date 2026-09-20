from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from dateutil.relativedelta import relativedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.text import get_valid_filename

from apps.sales.activity import record_activity
from apps.sales.models import (
    DocumentType,
    SalesDocument,
    SalesDocumentNumberSequence,
)
from apps.sales.models_lifecycle import (
    SalesAttachment,
    SalesCreditNote,
    SalesPayment,
    SalesRecurringInvoice,
    SalesScheduledDelivery,
)
from apps.sales.pdf_service import invalidate_document_pdf
from apps.sales.services import (
    next_document_number,
    snapshot_document_presentation,
)


MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
    ".txt",
    ".ppt",
    ".pptx",
}


def invoice_ledger(invoice):
    if invoice.document_type != DocumentType.INVOICE:
        raise ValidationError("Payments are available only for invoices.")

    succeeded = invoice.payments.filter(status=SalesPayment.Status.SUCCEEDED)
    paid = (
        succeeded.filter(kind=SalesPayment.Kind.PAYMENT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )
    refunded = (
        succeeded.filter(kind=SalesPayment.Kind.REFUND).aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )
    credits = (
        invoice.credit_notes.filter(
            status__in=[
                SalesCreditNote.Status.APPLIED,
                SalesCreditNote.Status.REFUNDED,
            ]
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )
    net_paid = max(Decimal("0"), paid - refunded)
    balance = max(Decimal("0"), Decimal(invoice.total) - net_paid - credits)
    return {
        "paid": paid,
        "refunded": refunded,
        "net_paid": net_paid,
        "credits": credits,
        "balance": balance,
    }


@transaction.atomic
def recalculate_invoice_status(invoice):
    invoice = SalesDocument.objects.select_for_update().get(pk=invoice.pk)
    if invoice.document_type != DocumentType.INVOICE:
        return invoice
    if invoice.status == SalesDocument.Status.CANCELLED:
        return invoice

    ledger = invoice_ledger(invoice)
    today = timezone.localdate()
    if ledger["balance"] <= 0:
        new_status = SalesDocument.Status.PAID
    elif ledger["net_paid"] > 0 or ledger["credits"] > 0:
        new_status = SalesDocument.Status.PARTIAL
    elif invoice.due_date and invoice.due_date < today:
        new_status = SalesDocument.Status.OVERDUE
    else:
        new_status = SalesDocument.Status.UNPAID

    if invoice.status != new_status:
        old = invoice.status
        invoice.status = new_status
        invoice.save(update_fields=["status", "updated_at"])
        record_activity(
            invoice,
            event_type="invoice_status_changed",
            message=f"Invoice status changed from {old} to {new_status}.",
            metadata={"old_status": old, "new_status": new_status},
        )
    return invoice


@transaction.atomic
def record_payment(
    *,
    invoice,
    amount,
    method,
    actor=None,
    payment_date=None,
    reference_number="",
    note="",
    provider="",
    external_payment_id="",
    checkout=None,
):
    invoice = SalesDocument.objects.select_for_update().get(
        pk=invoice.pk,
        organization=invoice.organization,
    )
    if invoice.document_type != DocumentType.INVOICE:
        raise ValidationError("Payments can only be recorded against invoices.")
    if invoice.status in {SalesDocument.Status.DRAFT, SalesDocument.Status.CANCELLED}:
        raise ValidationError("Send the invoice before recording payments.")
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount <= 0:
        raise ValidationError("Payment amount must be greater than zero.")

    payment = SalesPayment.objects.create(
        organization=invoice.organization,
        invoice=invoice,
        checkout=checkout,
        kind=SalesPayment.Kind.PAYMENT,
        status=SalesPayment.Status.SUCCEEDED,
        method=method,
        amount=amount,
        currency=invoice.currency,
        payment_date=payment_date or timezone.localdate(),
        reference_number=str(reference_number or "")[:160],
        external_payment_id=str(external_payment_id or "")[:255],
        provider=str(provider or "")[:20],
        note=str(note or ""),
        recorded_by=actor,
    )
    record_activity(
        invoice,
        event_type="payment_recorded",
        message=f"Payment of {invoice.currency} {amount:.2f} recorded.",
        actor=actor,
        metadata={
            "payment_id": str(payment.id),
            "method": payment.method,
            "provider": payment.provider,
        },
    )
    recalculate_invoice_status(invoice)
    return payment


@transaction.atomic
def record_refund(
    *,
    invoice,
    amount,
    actor=None,
    payment_date=None,
    reference_number="",
    note="",
    provider="",
    external_payment_id="",
):
    invoice = SalesDocument.objects.select_for_update().get(pk=invoice.pk)
    if invoice.document_type != DocumentType.INVOICE:
        raise ValidationError("Refunds can only be recorded against invoices.")
    if invoice.status in {SalesDocument.Status.DRAFT, SalesDocument.Status.CANCELLED}:
        raise ValidationError("Draft or cancelled invoices cannot be refunded.")
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount <= 0:
        raise ValidationError("Refund amount must be greater than zero.")

    ledger = invoice_ledger(invoice)
    if amount > ledger["net_paid"]:
        raise ValidationError("Refund cannot exceed the net amount paid.")

    refund = SalesPayment.objects.create(
        organization=invoice.organization,
        invoice=invoice,
        kind=SalesPayment.Kind.REFUND,
        status=SalesPayment.Status.SUCCEEDED,
        method=SalesPayment.Method.GATEWAY if provider else SalesPayment.Method.OTHER,
        amount=amount,
        currency=invoice.currency,
        payment_date=payment_date or timezone.localdate(),
        reference_number=str(reference_number or "")[:160],
        external_payment_id=str(external_payment_id or "")[:255],
        provider=str(provider or "")[:20],
        note=str(note or ""),
        recorded_by=actor,
    )
    record_activity(
        invoice,
        event_type="refund_recorded",
        message=f"Refund of {invoice.currency} {amount:.2f} recorded.",
        actor=actor,
        metadata={"refund_id": str(refund.id), "provider": refund.provider},
    )
    recalculate_invoice_status(invoice)
    return refund


@transaction.atomic
def _next_credit_number(organization):
    sequence, _ = SalesDocumentNumberSequence.objects.get_or_create(
        organization=organization,
        document_type="credit_note",
        defaults={"next_number": 1},
    )
    sequence = SalesDocumentNumberSequence.objects.select_for_update().get(pk=sequence.pk)
    value = sequence.next_number
    sequence.next_number = value + 1
    sequence.save(update_fields=["next_number"])
    return f"CN-{value:05d}"


@transaction.atomic
def create_credit_note(*, invoice, amount, reason="", actor=None, apply=True):
    invoice = SalesDocument.objects.select_for_update().get(pk=invoice.pk)
    if invoice.document_type != DocumentType.INVOICE:
        raise ValidationError("Credit notes can only be created for invoices.")
    if invoice.status in {SalesDocument.Status.DRAFT, SalesDocument.Status.CANCELLED}:
        raise ValidationError("Send the invoice before creating a credit note.")
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount <= 0:
        raise ValidationError("Credit note amount must be greater than zero.")

    ledger = invoice_ledger(invoice)
    if amount > ledger["balance"]:
        raise ValidationError("Credit note cannot exceed the current invoice balance.")

    note = SalesCreditNote.objects.create(
        organization=invoice.organization,
        invoice=invoice,
        credit_number=_next_credit_number(invoice.organization),
        amount=amount,
        reason=str(reason or ""),
        status=SalesCreditNote.Status.APPLIED if apply else SalesCreditNote.Status.DRAFT,
        created_by=actor,
        applied_at=timezone.now() if apply else None,
    )
    record_activity(
        invoice,
        event_type="credit_note_created",
        message=f"Credit note {note.credit_number} for {invoice.currency} {amount:.2f} created.",
        actor=actor,
        metadata={
            "credit_note_id": str(note.id),
            "credit_number": note.credit_number,
            "status": note.status,
        },
    )
    recalculate_invoice_status(invoice)
    return note


@transaction.atomic
def apply_credit_note(*, credit_note, actor=None):
    credit_note = SalesCreditNote.objects.select_for_update().select_related("invoice").get(
        pk=credit_note.pk
    )
    if credit_note.status != SalesCreditNote.Status.DRAFT:
        raise ValidationError("Only draft credit notes can be applied.")
    ledger = invoice_ledger(credit_note.invoice)
    if credit_note.amount > ledger["balance"]:
        raise ValidationError("Credit note exceeds the current invoice balance.")

    credit_note.status = SalesCreditNote.Status.APPLIED
    credit_note.applied_at = timezone.now()
    credit_note.save(update_fields=["status", "applied_at"])
    record_activity(
        credit_note.invoice,
        event_type="credit_note_applied",
        message=f"Credit note {credit_note.credit_number} applied.",
        actor=actor,
        metadata={"credit_note_id": str(credit_note.id)},
    )
    recalculate_invoice_status(credit_note.invoice)
    return credit_note


def validate_attachment(uploaded_file):
    if not uploaded_file:
        raise ValidationError("Choose a file to upload.")
    if uploaded_file.size <= 0:
        raise ValidationError("Attachment is empty.")
    if uploaded_file.size > MAX_ATTACHMENT_BYTES:
        raise ValidationError("Attachments must be 25 MB or smaller.")
    original_name = Path(uploaded_file.name or "attachment").name
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise ValidationError("This attachment file type is not supported.")
    filename = get_valid_filename(original_name) or f"attachment{extension}"
    uploaded_file.name = filename
    return (
        filename,
        str(getattr(uploaded_file, "content_type", "") or "application/octet-stream")[:120],
    )


def add_attachment(*, document, uploaded_file, actor=None, visible_to_customer=False):
    filename, mime_type = validate_attachment(uploaded_file)
    attachment = SalesAttachment.objects.create(
        organization=document.organization,
        document=document,
        file=uploaded_file,
        original_name=filename,
        mime_type=mime_type,
        size=uploaded_file.size,
        visible_to_customer=bool(visible_to_customer),
        uploaded_by=actor,
    )
    record_activity(
        document,
        event_type="attachment_added",
        message=f"Attachment {filename} added.",
        actor=actor,
        metadata={
            "attachment_id": str(attachment.id),
            "customer_visible": attachment.visible_to_customer,
        },
    )
    return attachment


@transaction.atomic
def create_agreement_revision(*, agreement, kind, actor=None):
    agreement = SalesDocument.objects.select_for_update().select_related(
        "template", "lead"
    ).get(pk=agreement.pk)
    if agreement.document_type != DocumentType.AGREEMENT:
        raise ValidationError("Only agreements can be amended or renewed.")
    if agreement.status == SalesDocument.Status.DRAFT:
        raise ValidationError("Edit the draft directly instead of creating a revision.")
    if kind not in {"amendment", "renewal"}:
        raise ValidationError("Agreement revision type is invalid.")

    agreement.is_current_version = False
    agreement.save(update_fields=["is_current_version", "updated_at"])

    number = next_document_number(
        organization=agreement.organization,
        document_type=DocumentType.AGREEMENT,
        prefix=(agreement.template.number_prefix if agreement.template_id else ""),
    )
    new_document = SalesDocument.objects.create(
        organization=agreement.organization,
        document_type=DocumentType.AGREEMENT,
        template=agreement.template,
        source_document=agreement,
        supersedes=agreement,
        revision_number=agreement.revision_number + 1,
        revision_kind=kind,
        is_current_version=True,
        lead=agreement.lead,
        created_by=actor,
        document_number=number,
        title=agreement.title,
        recipient_name=agreement.recipient_name,
        recipient_email=agreement.recipient_email,
        recipient_phone=agreement.recipient_phone,
        currency=agreement.currency,
        issue_date=timezone.localdate(),
        valid_until=agreement.valid_until,
        line_items=agreement.line_items,
        subtotal=agreement.subtotal,
        tax_total=agreement.tax_total,
        discount_total=agreement.discount_total,
        total=agreement.total,
        content=agreement.content,
        terms=agreement.terms,
        layout_override=agreement.layout_override,
    )
    snapshot_document_presentation(new_document)
    new_document.save()
    record_activity(
        agreement,
        event_type=f"agreement_{kind}_created",
        message=f"{kind.title()} draft {new_document.document_number} created.",
        actor=actor,
        metadata={"new_document_id": str(new_document.id)},
    )
    record_activity(
        new_document,
        event_type="agreement_revision_created",
        message=f"Created from {agreement.document_number}.",
        actor=actor,
        metadata={
            "previous_document_id": str(agreement.id),
            "revision_number": new_document.revision_number,
            "revision_kind": kind,
        },
    )
    return new_document


def create_scheduled_delivery(
    *,
    document,
    channels,
    scheduled_at,
    email_subject="",
    email_body="",
    whatsapp_body="",
    base_url="",
    actor=None,
):
    clean_channels = [
        channel
        for channel in channels
        if channel in {"email", "whatsapp"}
    ]
    clean_channels = list(dict.fromkeys(clean_channels))
    if not clean_channels:
        raise ValidationError("Select at least one delivery channel.")
    if scheduled_at <= timezone.now():
        raise ValidationError("Scheduled send time must be in the future.")

    schedule = SalesScheduledDelivery.objects.create(
        organization=document.organization,
        document=document,
        channels=clean_channels,
        email_subject=str(email_subject or "")[:255],
        email_body=str(email_body or ""),
        whatsapp_body=str(whatsapp_body or ""),
        base_url=str(base_url or "")[:2048],
        scheduled_at=scheduled_at,
        created_by=actor,
    )
    record_activity(
        document,
        event_type="delivery_scheduled",
        message=f"Document scheduled for {scheduled_at.isoformat()}.",
        actor=actor,
        metadata={
            "schedule_id": str(schedule.id),
            "channels": clean_channels,
        },
    )
    return schedule


def _advance_recurrence(rule):
    count = max(1, int(rule.interval_count))
    if rule.interval_unit == SalesRecurringInvoice.IntervalUnit.DAY:
        return rule.next_run_at + timedelta(days=count)
    if rule.interval_unit == SalesRecurringInvoice.IntervalUnit.WEEK:
        return rule.next_run_at + timedelta(weeks=count)
    if rule.interval_unit == SalesRecurringInvoice.IntervalUnit.MONTH:
        return rule.next_run_at + relativedelta(months=count)
    return rule.next_run_at + relativedelta(years=count)


@transaction.atomic
def create_recurring_invoice_instance(rule):
    rule = SalesRecurringInvoice.objects.select_for_update().select_related(
        "source_invoice__template",
        "source_invoice__lead",
    ).get(pk=rule.pk)
    now = timezone.now()
    if not rule.is_active or rule.next_run_at > now:
        return None
    if rule.ends_at and rule.next_run_at > rule.ends_at:
        rule.is_active = False
        rule.save(update_fields=["is_active", "updated_at"])
        return None
    if rule.max_cycles is not None and rule.cycles_created >= rule.max_cycles:
        rule.is_active = False
        rule.save(update_fields=["is_active", "updated_at"])
        return None

    source = rule.source_invoice
    number = next_document_number(
        organization=source.organization,
        document_type=DocumentType.INVOICE,
        prefix=(source.template.number_prefix if source.template_id else ""),
    )
    due_offset = (
        source.due_date - source.issue_date
        if source.due_date and source.issue_date
        else timedelta(days=0)
    )
    today = timezone.localdate()
    invoice = SalesDocument.objects.create(
        organization=source.organization,
        document_type=DocumentType.INVOICE,
        template=source.template,
        source_document=source,
        lead=source.lead,
        created_by=rule.created_by,
        document_number=number,
        title=source.title,
        recipient_name=source.recipient_name,
        recipient_email=source.recipient_email,
        recipient_phone=source.recipient_phone,
        currency=source.currency,
        issue_date=today,
        due_date=today + due_offset,
        line_items=source.line_items,
        subtotal=source.subtotal,
        tax_total=source.tax_total,
        discount_total=source.discount_total,
        total=source.total,
        content=source.content,
        terms=source.terms,
        layout_override=source.layout_override,
    )
    snapshot_document_presentation(invoice)
    invoice.save()

    rule.cycles_created += 1
    rule.next_run_at = _advance_recurrence(rule)
    if (
        (rule.max_cycles is not None and rule.cycles_created >= rule.max_cycles)
        or (rule.ends_at and rule.next_run_at > rule.ends_at)
    ):
        rule.is_active = False
    rule.save(
        update_fields=[
            "cycles_created",
            "next_run_at",
            "is_active",
            "updated_at",
        ]
    )
    record_activity(
        invoice,
        event_type="recurring_invoice_created",
        message=f"Recurring invoice {invoice.document_number} generated.",
        actor=rule.created_by,
        metadata={
            "recurring_rule_id": str(rule.id),
            "cycle": rule.cycles_created,
        },
    )
    return invoice


def disable_recurring_rule(rule, *, actor=None):
    rule.is_active = False
    rule.save(update_fields=["is_active", "updated_at"])
    record_activity(
        rule.source_invoice,
        event_type="recurring_invoice_disabled",
        message="Recurring invoice schedule disabled.",
        actor=actor,
        metadata={"recurring_rule_id": str(rule.id)},
    )
    return rule


def invalidate_draft_artifacts(document):
    if document.status != SalesDocument.Status.DRAFT:
        raise ValidationError("Only draft document artifacts can be invalidated.")
    invalidate_document_pdf(document)
