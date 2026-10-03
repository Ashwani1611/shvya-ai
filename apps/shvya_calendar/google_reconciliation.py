"""Import edits to known Google events without overwriting local pending changes."""
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from apps.crm.models import Lead
from .google import (
    ACTIVE, GoogleCalendarError, _admit_google_request,
    _event_url, _google_error, _google_request, _headers, _response_json,
    _save_event_details,
)
from .models import CalendarBooking, CalendarPage, CalendarReminderDelivery
from .platform_google import check_platform_event, connection_for_booking


def _event_time(value, fallback_zone):
    if not isinstance(value, dict):
        raise GoogleCalendarError("Google returned an invalid event time.")
    try:
        result = datetime.fromisoformat(str(value.get("dateTime") or value["date"]).replace("Z", "+00:00"))
        if timezone.is_naive(result):
            result = result.replace(tzinfo=ZoneInfo(value.get("timeZone") or fallback_zone))
        return result
    except (KeyError, ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise GoogleCalendarError("Google returned an invalid event time.") from exc


def import_google_booking(booking):
    if not booking.google_event_id or booking.status not in ACTIVE:
        return "inactive"
    if booking.calendar_sync_status != CalendarBooking.SyncStatus.SYNCED:
        return "pending_local"
    connection = connection_for_booking(booking)
    if connection is None:
        return "not_connected"
    revision = booking.updated_at
    _admit_google_request(connection)
    response = _google_request(
        "GET", _event_url(booking, connection), headers=_headers(connection), timeout=20,
        failure_message="Google Calendar changes are temporarily unavailable.",
    )
    if not response.ok:
        # A 404 can mean lost access or a moved calendar. Preserve the CRM row.
        raise _google_error(response, f"Unable to read Google event changes ({response.status_code}).")
    event = _response_json(response, failure_message="Google returned an invalid event.")
    if event.get("id") != booking.google_event_id:
        raise GoogleCalendarError("Google event did not match this booking.")
    # Cancelled events may contain only an ID. The exact event was requested
    # through this booking's original credentials and persisted calendar ID.
    cancelled = event.get("status") == "cancelled"
    if not cancelled:
        check_platform_event(booking, event)
        start = _event_time(event.get("start"), booking.timezone)
        end = _event_time(event.get("end"), booking.timezone)
        if start >= end:
            raise GoogleCalendarError("Google returned an invalid event duration.")
    with transaction.atomic():
        Lead.objects.select_for_update().get(pk=booking.lead_id, organization_id=booking.organization_id)
        CalendarPage.objects.select_for_update().get(pk=booking.page_id, organization_id=booking.organization_id)
        current = CalendarBooking.objects.select_for_update().get(pk=booking.pk, organization_id=booking.organization_id)
        if current.updated_at != revision or current.calendar_sync_status != CalendarBooking.SyncStatus.SYNCED or current.status not in ACTIVE:
            return "changed_locally"
        if cancelled:
            current.status = CalendarBooking.Status.CANCELLED
            current.cancelled_at = timezone.now()
            current.meeting_link = ""
            current.google_event_url = ""
            current.save(update_fields=["status", "cancelled_at", "meeting_link", "google_event_url", "updated_at"])
            CalendarReminderDelivery.objects.filter(booking=current).exclude(
                status__in=[CalendarReminderDelivery.Status.SENT, CalendarReminderDelivery.Status.COMPLETED],
            ).update(status=CalendarReminderDelivery.Status.SKIPPED, error="Booking cancelled in Google Calendar.")
            return "cancelled"
        changed = current.start_at != start or current.end_at != end
        if changed:
            current.previous_start_at, current.previous_end_at = current.start_at, current.end_at
            current.start_at, current.end_at = start, end
            current.status = CalendarBooking.Status.RESCHEDULED
            current.save(update_fields=["previous_start_at", "previous_end_at", "start_at", "end_at", "status", "updated_at"])
        _save_event_details(current, event)
        if changed:
            from .reminder_services import schedule_booking_reminders
            schedule_booking_reminders(current)
        CalendarBooking.objects.filter(pk=current.pk).update(calendar_sync_error="")
    return "updated" if changed else "unchanged"
