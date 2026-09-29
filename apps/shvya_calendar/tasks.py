import logging

from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from .models import CalendarBooking, CalendarReminderDelivery

logger = logging.getLogger(__name__)


def _google_retry_delay(*, booking_id, retries, retry_after=None):
    if retry_after is not None:
        try:
            base = max(1, min(int(retry_after), 900))
        except (TypeError, ValueError):
            base = 5
    else:
        base = min(180, 5 * (2 ** max(0, int(retries or 0))))
    jitter = (
        sum(ord(character) for character in str(booking_id))
        + (int(retries or 0) * 7)
    ) % 7
    return min(907, base + jitter)


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
    from .reminder_services import render_booking_text
    delivery.rendered_subject = render_booking_text(step.subject, booking)
    delivery.rendered_body = render_booking_text(step.body, booking)
    delivery.save(update_fields=["rendered_subject", "rendered_body", "updated_at"])

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


@shared_task(
    bind=True,
    max_retries=6,
    default_retry_delay=5,
    name="shvya_calendar.refresh_booking_conference",
)
def refresh_booking_conference(self, booking_id):
    booking = (
        CalendarBooking.objects
        .select_related("page", "organization", "host", "lead")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None:
        return {"status": "missing"}
    if booking.status == CalendarBooking.Status.CANCELLED:
        return {"status": "cancelled"}
    if booking.meeting_link:
        return {"status": "synced", "meeting_link": booking.meeting_link}
    if (
        booking.page.meeting_location
        != booking.page.MeetingLocation.GOOGLE_MEET
    ):
        return {"status": "not_google_meet"}

    from .google import GoogleCalendarError, refresh_booking_event_details

    try:
        booking = refresh_booking_event_details(booking)
        if not booking.meeting_link:
            raise GoogleCalendarError(
                "Google Meet conference is still being prepared.",
                transient=True,
            )
        return {"status": "synced", "meeting_link": booking.meeting_link}
    except GoogleCalendarError as exc:
        retryable = bool(getattr(exc, "transient", False))
        exhausted = self.request.retries >= self.max_retries
        CalendarBooking.objects.filter(pk=booking.pk).update(
            calendar_sync_status=(
                CalendarBooking.SyncStatus.PENDING
                if retryable and not exhausted
                else CalendarBooking.SyncStatus.FAILED
            ),
            calendar_sync_error=str(exc)[:1000],
        )
        if retryable and not exhausted:
            countdown = _google_retry_delay(
                booking_id=booking.pk,
                retries=self.request.retries,
                retry_after=getattr(exc, "retry_after", None),
            )
            if getattr(exc, "status_code", None) == 429:
                from apps.core.observability import increment

                increment(
                    "provider.throttled",
                    labels={"provider": "google_calendar"},
                )
            raise self.retry(exc=exc, countdown=countdown)
        return {
            "status": "failed",
            "error": str(exc),
        }


@shared_task(name="shvya_calendar.recover_pending_google_meet")
def recover_pending_google_meet():
    from .platform_google import platform_status
    platform = platform_status()
    if platform["enabled"] and platform["configured"]:
        # Environment activation does not emit signals. Recover only future,
        # unconnected Meet bookings in explicitly opted-in organisations;
        # never touch an existing host-owned event or silently opt a tenant in.
        eligible = list(CalendarBooking.objects.filter(
            organization__is_active=True,
            organization__settings__calendar_google__allow_platform_fallback=True,
            status__in=[CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED],
            calendar_sync_status=CalendarBooking.SyncStatus.NOT_CONNECTED,
            page__meeting_location="google_meet", google_event_id="",
            start_at__gt=timezone.now(),
        ).values_list("pk", flat=True)[:100])
        CalendarBooking.objects.filter(
            pk__in=eligible, google_event_id="",
            calendar_sync_status=CalendarBooking.SyncStatus.NOT_CONNECTED,
            organization__is_active=True,
            organization__settings__calendar_google__allow_platform_fallback=True,
        ).update(
            calendar_sync_status=CalendarBooking.SyncStatus.PENDING,
        )
    unsynced_ids = list(CalendarBooking.objects.filter(
        status__in=[CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED],
        calendar_sync_status=CalendarBooking.SyncStatus.PENDING,
        start_at__gt=timezone.now(),
    ).filter(
        models.Q(google_event_id="") | ~models.Q(meeting_link="") |
        ~models.Q(page__meeting_location="google_meet")
    ).values_list("id", flat=True)[:100])
    for booking_id in unsynced_ids:
        sync_booking_calendar.delay(str(booking_id))
    booking_ids = list(
        CalendarBooking.objects
        .filter(
            status__in=[
                CalendarBooking.Status.SCHEDULED,
                CalendarBooking.Status.RESCHEDULED,
            ],
            calendar_sync_status=CalendarBooking.SyncStatus.PENDING,
            meeting_link="", start_at__gt=timezone.now(),
        )
        .exclude(google_event_id="")
        .values_list("id", flat=True)[:100]
    )
    for booking_id in booking_ids:
        refresh_booking_conference.delay(str(booking_id))
    return {"queued": len(booking_ids) + len(unsynced_ids)}


@shared_task(bind=True, max_retries=5, name="shvya_calendar.sync_booking_calendar")
def sync_booking_calendar(self, booking_id):
    """Retry-safe Google create/update after the CRM reservation commits."""
    from .google import GoogleCalendarError, update_booking_event
    booking = CalendarBooking.objects.select_related("page", "lead", "host", "organization").filter(pk=booking_id).first()
    if booking is None or booking.status not in (CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED):
        return {"status": "inactive"}
    try:
        update_booking_event(booking)
    except GoogleCalendarError as exc:
        retry = exc.transient and self.request.retries < self.max_retries
        CalendarBooking.objects.filter(pk=booking.pk).update(
            calendar_sync_status=CalendarBooking.SyncStatus.PENDING if retry else CalendarBooking.SyncStatus.FAILED,
            calendar_sync_error=str(exc)[:1000],
        )
        if retry:
            raise self.retry(exc=exc, countdown=_google_retry_delay(
                booking_id=booking.pk, retries=self.request.retries, retry_after=exc.retry_after,
            ))
        return {"status": "failed"}
    return {"status": booking.calendar_sync_status}
