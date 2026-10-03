"""Keep cancelled appointments for history and delete their provider event."""
from django.db import transaction
from django.utils import timezone

from apps.crm.models import Lead
from .attribute_sync import enqueue_booking_sync
from .models import CalendarBooking, CalendarReminderDelivery


def cancel_booking(booking):
    with transaction.atomic():
        Lead.objects.select_for_update().get(pk=booking.lead_id, organization_id=booking.organization_id)
        current = CalendarBooking.objects.select_for_update().get(pk=booking.pk, organization_id=booking.organization_id)
        if current.status == CalendarBooking.Status.CANCELLED:
            return current
        current.status = CalendarBooking.Status.CANCELLED
        current.cancelled_at = timezone.now()
        current.calendar_sync_status = (
            CalendarBooking.SyncStatus.PENDING if current.google_event_id
            else CalendarBooking.SyncStatus.SYNCED
        )
        current.calendar_sync_error = ""
        current.meeting_link = ""
        current.google_event_url = ""
        current.save(update_fields=["status", "cancelled_at", "calendar_sync_status", "calendar_sync_error", "meeting_link", "google_event_url", "updated_at"])
        CalendarReminderDelivery.objects.filter(booking=current).exclude(
            status__in=[CalendarReminderDelivery.Status.SENT, CalendarReminderDelivery.Status.COMPLETED],
        ).update(status=CalendarReminderDelivery.Status.SKIPPED, error="Booking cancelled.")
        if current.google_event_id:
            transaction.on_commit(lambda: enqueue_booking_sync(current.pk))
    return current
