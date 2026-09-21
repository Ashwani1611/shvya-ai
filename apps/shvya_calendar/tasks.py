import logging

from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from .models import CalendarBooking, CalendarReminderDelivery

logger = logging.getLogger(__name__)


def _strict_whatsapp_account(booking):
    """Resolve only the WhatsApp connection owned by the lead's pipeline.

    Calendar reminders must never fall back to an unrelated connected number.
    """
    from apps.channels.models import WhatsAppAccount

    lead = booking.lead
    pipeline_number = str(getattr(lead.pipeline, "phone_number", "") or "").strip()
    if not pipeline_number:
        return None

    return (
        WhatsAppAccount.objects
        .filter(
            organization=booking.organization,
            is_active=True,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        .filter(
            models.Q(display_phone_number=pipeline_number)
            | models.Q(phone_number_id=pipeline_number)
        )
        .first()
    )


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="shvya_calendar.dispatch_reminder",
)
def dispatch_calendar_reminder(self, delivery_id):
    with transaction.atomic():
        delivery = (
            CalendarReminderDelivery.objects
            .select_for_update()
            .select_related(
                "booking",
                "booking__lead",
                "booking__lead__pipeline",
                "booking__organization",
                "step",
            )
            .filter(pk=delivery_id)
            .first()
        )
        if delivery is None:
            return {"status": "missing"}
        if delivery.status not in {
            CalendarReminderDelivery.Status.PENDING,
            CalendarReminderDelivery.Status.FAILED,
        }:
            return {"status": delivery.status}
        if delivery.booking.status == CalendarBooking.Status.CANCELLED:
            delivery.status = CalendarReminderDelivery.Status.SKIPPED
            delivery.error = "Booking was cancelled."
            delivery.save(update_fields=["status", "error", "updated_at"])
            return {"status": "skipped"}
        if delivery.due_at > timezone.now():
            return {"status": "not_due"}

        delivery.status = CalendarReminderDelivery.Status.PROCESSING
        delivery.error = ""
        delivery.save(update_fields=["status", "error", "updated_at"])

    step = delivery.step
    booking = delivery.booking

    try:
        if step.channel == step.Channel.CALL_REMINDER:
            CalendarReminderDelivery.objects.filter(pk=delivery.pk).update(
                status=CalendarReminderDelivery.Status.ATTENTION,
                error="",
            )
            return {"status": "attention"}

        if step.channel == step.Channel.EMAIL:
            if not booking.lead.email:
                CalendarReminderDelivery.objects.filter(pk=delivery.pk).update(
                    status=CalendarReminderDelivery.Status.SKIPPED,
                    error="Lead has no email address.",
                )
                return {"status": "skipped"}
            from apps.integrations.services.email import send_organization_email

            sent = send_organization_email(
                organization=booking.organization,
                to=booking.lead.email,
                subject=delivery.rendered_subject or booking.page.session_title,
                text_body=delivery.rendered_body,
            )
            if not sent:
                raise RuntimeError(
                    "The connected organization mailbox did not send the reminder."
                )

        elif step.channel == step.Channel.WHATSAPP:
            account = _strict_whatsapp_account(booking)
            if account is None:
                CalendarReminderDelivery.objects.filter(pk=delivery.pk).update(
                    status=CalendarReminderDelivery.Status.SKIPPED,
                    error=(
                        "No connected WhatsApp number is linked to this "
                        "lead's current pipeline."
                    ),
                )
                return {"status": "skipped"}

            from apps.channels.tasks import send_whatsapp_message_task
            from services.channels.whatsapp_service import queue_outbound_message

            message = queue_outbound_message(
                organization=booking.organization,
                account=account,
                lead=booking.lead,
                to_number=booking.lead.phone,
                body=delivery.rendered_body,
            )
            send_whatsapp_message_task.delay(str(message.id))
        else:
            raise ValueError("Unsupported SHVYA Calendar reminder channel.")

        CalendarReminderDelivery.objects.filter(pk=delivery.pk).update(
            status=CalendarReminderDelivery.Status.SENT,
            sent_at=timezone.now(),
            error="",
        )
        return {"status": "sent"}

    except Exception as exc:
        logger.exception(
            "SHVYA Calendar reminder %s failed",
            delivery_id,
        )
        CalendarReminderDelivery.objects.filter(pk=delivery.pk).update(
            status=CalendarReminderDelivery.Status.FAILED,
            error=str(exc)[:1000],
        )
        raise self.retry(exc=exc)


@shared_task(name="shvya_calendar.dispatch_due_reminders")
def dispatch_due_calendar_reminders():
    due_ids = list(
        CalendarReminderDelivery.objects
        .filter(
            status=CalendarReminderDelivery.Status.PENDING,
            due_at__lte=timezone.now(),
            booking__status__in=[
                CalendarBooking.Status.SCHEDULED,
                CalendarBooking.Status.RESCHEDULED,
            ],
        )
        .values_list("id", flat=True)[:250]
    )
    for delivery_id in due_ids:
        dispatch_calendar_reminder.delay(str(delivery_id))
    return {"queued": len(due_ids)}
