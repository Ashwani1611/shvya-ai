from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urljoin

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.sales.activity import record_activity
from apps.sales.lifecycle import (
    create_recurring_invoice_instance,
    recalculate_invoice_status,
)
from apps.sales.models import DocumentType, SalesDocument
from apps.sales.models_lifecycle import (
    SalesRecurringInvoice,
    SalesReminder,
    SalesScheduledDelivery,
    SalesSettings,
)
from apps.sales.services import (
    SalesDeliveryError,
    deliver_email,
    deliver_whatsapp,
    delivery_drafts,
)


logger = logging.getLogger(__name__)


def _base_url(value=""):
    return str(value or getattr(settings, "SALES_PUBLIC_BASE_URL", "") or "").rstrip("/")


def _public_url(document, base_url):
    from django.urls import reverse

    return urljoin(
        base_url + "/",
        reverse("shvya-sales-public-document", args=[document.public_token]).lstrip("/"),
    )


@shared_task(name="sales.dispatch_scheduled", ignore_result=True)
def dispatch_scheduled_deliveries_task():
    now = timezone.now()
    stale = now - timedelta(minutes=10)
    due_ids = list(
        SalesScheduledDelivery.objects.filter(
            status=SalesScheduledDelivery.Status.PENDING,
            scheduled_at__lte=now,
        ).values_list("pk", flat=True)[:50]
    )
    # Recover an interrupted claim after a bounded lease.
    due_ids.extend(
        SalesScheduledDelivery.objects.filter(
            status=SalesScheduledDelivery.Status.PROCESSING,
            claimed_at__lt=stale,
            scheduled_at__lte=now,
        ).values_list("pk", flat=True)[:20]
    )

    processed = 0
    for schedule_id in dict.fromkeys(due_ids):
        with transaction.atomic():
            schedule = (
                SalesScheduledDelivery.objects.select_for_update()
                .select_related("document", "created_by")
                .filter(pk=schedule_id)
                .first()
            )
            if schedule is None or schedule.status == SalesScheduledDelivery.Status.CANCELLED:
                continue
            if schedule.scheduled_at > timezone.now():
                continue
            schedule.status = SalesScheduledDelivery.Status.PROCESSING
            schedule.claimed_at = timezone.now()
            schedule.error_message = ""
            schedule.save(
                update_fields=[
                    "status",
                    "claimed_at",
                    "error_message",
                ]
            )

        base_url = _base_url(schedule.base_url)
        successes = []
        errors = []
        for channel in schedule.channels or []:
            try:
                if channel == "email":
                    deliver_email(
                        document=schedule.document,
                        user=schedule.created_by,
                        subject=schedule.email_subject,
                        body=schedule.email_body,
                        base_url=base_url,
                        attach_pdf=True,
                    )
                    successes.append("email")
                elif channel == "whatsapp":
                    deliver_whatsapp(
                        document=schedule.document,
                        user=schedule.created_by,
                        body=schedule.whatsapp_body,
                        base_url=base_url,
                        attach_pdf=True,
                    )
                    successes.append("whatsapp")
            except SalesDeliveryError as exc:
                errors.append(f"{channel}: {exc}")
            except Exception as exc:
                logger.exception("Scheduled SHVYA Sales delivery failed: %s", schedule_id)
                errors.append(f"{channel}: delivery failed")

        with transaction.atomic():
            schedule = SalesScheduledDelivery.objects.select_for_update().get(pk=schedule_id)
            schedule.dispatched_at = timezone.now()
            schedule.error_message = "\n".join(errors)[:3000]
            if successes and errors:
                schedule.status = SalesScheduledDelivery.Status.PARTIAL
            elif successes:
                schedule.status = SalesScheduledDelivery.Status.COMPLETED
            else:
                schedule.status = SalesScheduledDelivery.Status.FAILED
            schedule.save(
                update_fields=[
                    "status",
                    "error_message",
                    "dispatched_at",
                ]
            )
        record_activity(
            schedule.document,
            event_type="scheduled_delivery_completed",
            message=(
                f"Scheduled delivery completed via {', '.join(successes)}."
                if successes
                else "Scheduled delivery failed."
            ),
            actor=schedule.created_by,
            metadata={
                "schedule_id": str(schedule.id),
                "successful_channels": successes,
                "failed_channels": [item.split(":", 1)[0] for item in errors],
            },
        )
        processed += 1
    return processed


def _settings_for(document):
    settings_row, _ = SalesSettings.objects.get_or_create(
        organization=document.organization
    )
    return settings_row


def _ensure_reminder(document, kind, due_on):
    reminder, _ = SalesReminder.objects.get_or_create(
        organization=document.organization,
        document=document,
        kind=kind,
        due_on=due_on,
    )
    return reminder


def _send_email_reminder(document, reminder, *, subject, body, base_url):
    if reminder.status == SalesReminder.Status.SENT:
        return
    if not document.recipient_email:
        reminder.status = SalesReminder.Status.SKIPPED
        reminder.error_message = "Recipient has no email address."
        reminder.save(update_fields=["status", "error_message"])
        return
    if not base_url:
        reminder.status = SalesReminder.Status.SKIPPED
        reminder.error_message = "SALES_PUBLIC_BASE_URL is not configured."
        reminder.save(update_fields=["status", "error_message"])
        return

    try:
        deliver_email(
            document=document,
            user=None,
            subject=subject,
            body=body,
            base_url=base_url,
            attach_pdf=True,
        )
    except SalesDeliveryError as exc:
        reminder.status = SalesReminder.Status.FAILED
        reminder.error_message = str(exc)[:1500]
        reminder.save(update_fields=["status", "error_message"])
        return

    reminder.status = SalesReminder.Status.SENT
    reminder.sent_at = timezone.now()
    reminder.error_message = ""
    reminder.save(update_fields=["status", "sent_at", "error_message"])
    record_activity(
        document,
        event_type="reminder_sent",
        message=f"{reminder.get_kind_display()} reminder sent.",
        metadata={
            "reminder_id": str(reminder.id),
            "kind": reminder.kind,
        },
    )


def _maintain_document(document, today, base_url):
    settings_row = _settings_for(document)

    if (
        document.document_type == DocumentType.QUOTATION
        and document.status == SalesDocument.Status.SENT
        and document.valid_until
    ):
        if document.valid_until < today:
            document.status = SalesDocument.Status.EXPIRED
            document.save(update_fields=["status", "updated_at"])
            record_activity(
                document,
                event_type="quotation_expired",
                message=f"{document.document_number} expired.",
            )
            return
        days_left = (document.valid_until - today).days
        if (
            settings_row.automatic_email_reminders
            and days_left == settings_row.quotation_expiry_reminder_days
        ):
            reminder = _ensure_reminder(
                document,
                SalesReminder.Kind.QUOTATION_EXPIRY,
                document.valid_until,
            )
            _send_email_reminder(
                document,
                reminder,
                subject=f"Quotation {document.document_number} expires soon",
                body=(
                    f"Hi {document.recipient_name or 'there'},\n\n"
                    f"Quotation {document.document_number} expires on "
                    f"{document.valid_until:%d %b %Y}.\n\n"
                    f"Review it here: {_public_url(document, base_url)}"
                ),
                base_url=base_url,
            )

    if (
        document.document_type == DocumentType.AGREEMENT
        and document.status in {
            SalesDocument.Status.SENT,
            SalesDocument.Status.SIGNED,
        }
        and document.valid_until
    ):
        if document.valid_until < today:
            document.status = SalesDocument.Status.EXPIRED
            document.save(update_fields=["status", "updated_at"])
            record_activity(
                document,
                event_type="agreement_expired",
                message=f"{document.document_number} expired.",
            )
            return
        days_left = (document.valid_until - today).days
        if (
            settings_row.automatic_email_reminders
            and days_left == settings_row.agreement_expiry_reminder_days
        ):
            reminder = _ensure_reminder(
                document,
                SalesReminder.Kind.AGREEMENT_EXPIRY,
                document.valid_until,
            )
            _send_email_reminder(
                document,
                reminder,
                subject=f"Agreement {document.document_number} expires soon",
                body=(
                    f"Hi {document.recipient_name or 'there'},\n\n"
                    f"Agreement {document.document_number} reaches its end/review date on "
                    f"{document.valid_until:%d %b %Y}.\n\n"
                    f"View it here: {_public_url(document, base_url)}"
                ),
                base_url=base_url,
            )

    if document.document_type == DocumentType.INVOICE:
        invoice = recalculate_invoice_status(document)
        if not invoice.due_date or invoice.status in {
            SalesDocument.Status.PAID,
            SalesDocument.Status.CANCELLED,
        }:
            return
        days_left = (invoice.due_date - today).days
        if (
            settings_row.automatic_email_reminders
            and days_left == settings_row.invoice_due_reminder_days
        ):
            reminder = _ensure_reminder(
                invoice,
                SalesReminder.Kind.INVOICE_DUE,
                invoice.due_date,
            )
            _send_email_reminder(
                invoice,
                reminder,
                subject=f"Invoice {invoice.document_number} is due soon",
                body=(
                    f"Hi {invoice.recipient_name or 'there'},\n\n"
                    f"Invoice {invoice.document_number} is due on "
                    f"{invoice.due_date:%d %b %Y}.\n\n"
                    f"View invoice: {_public_url(invoice, base_url)}"
                ),
                base_url=base_url,
            )
        elif days_left < 0 and settings_row.automatic_email_reminders:
            repeat_days = max(1, settings_row.invoice_overdue_repeat_days)
            days_overdue = abs(days_left)
            if days_overdue == 1 or days_overdue % repeat_days == 0:
                reminder = _ensure_reminder(
                    invoice,
                    SalesReminder.Kind.INVOICE_OVERDUE,
                    today,
                )
                _send_email_reminder(
                    invoice,
                    reminder,
                    subject=f"Invoice {invoice.document_number} is overdue",
                    body=(
                        f"Hi {invoice.recipient_name or 'there'},\n\n"
                        f"Invoice {invoice.document_number} is overdue by "
                        f"{days_overdue} day{'s' if days_overdue != 1 else ''}.\n\n"
                        f"View invoice: {_public_url(invoice, base_url)}"
                    ),
                    base_url=base_url,
                )


@shared_task(name="sales.maintain_documents", ignore_result=True)
def maintain_sales_documents_task():
    today = timezone.localdate()
    base_url = _base_url()
    documents = (
        SalesDocument.objects.filter(
            status__in=[
                SalesDocument.Status.SENT,
                SalesDocument.Status.SIGNED,
                SalesDocument.Status.UNPAID,
                SalesDocument.Status.PARTIAL,
                SalesDocument.Status.OVERDUE,
            ]
        )
        .select_related("organization", "lead", "template")
        .order_by("updated_at")[:500]
    )
    for document in documents:
        try:
            _maintain_document(document, today, base_url)
        except Exception:
            logger.exception("SHVYA Sales maintenance failed for %s", document.pk)

    due_rules = SalesRecurringInvoice.objects.filter(
        is_active=True,
        next_run_at__lte=timezone.now(),
    ).select_related("source_invoice")[:50]
    for rule in due_rules:
        try:
            invoice = create_recurring_invoice_instance(rule)
            if invoice and rule.auto_send_email and invoice.recipient_email and base_url:
                drafts = delivery_drafts(
                    invoice,
                    public_url=_public_url(invoice, base_url),
                )
                deliver_email(
                    document=invoice,
                    user=rule.created_by,
                    subject=drafts["email_subject"],
                    body=drafts["email_body"],
                    base_url=base_url,
                    attach_pdf=True,
                )
        except Exception:
            logger.exception("Recurring invoice generation failed for %s", rule.pk)
    return len(documents)
