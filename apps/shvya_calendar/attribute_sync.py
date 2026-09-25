"""Bidirectional CRM appointment mapping. Provider work remains asynchronous."""
from datetime import UTC, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.crm.models import AttributeDefinition, Lead
from .availability import _page_zone, available_slots
from .models import CalendarBooking, CalendarPage, CalendarSubmission


ACTIVE = (CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED)


def ensure_booked_at(organization_id):
    label = "Booked at"
    if AttributeDefinition.objects.filter(organization_id=organization_id, name=label).exclude(key="booked_at").exists():
        label = "Booked at (Calendar)"
    attribute, _ = AttributeDefinition.objects.get_or_create(
        organization_id=organization_id, key="booked_at",
        defaults={"name": label, "field_type": "datetime", "description": "Appointment time in the booking calendar timezone."},
    )
    if attribute.field_type != "datetime" or not attribute.is_active:
        attribute.field_type = "datetime"
        attribute.is_active = True
        attribute.save(update_fields=["field_type", "is_active", "updated_at"])
    return attribute


def map_booking_to_lead(booking):
    """Avoid Lead.save recursion; lock before merging other CRM attributes."""
    ensure_booked_at(booking.organization_id)
    with transaction.atomic():
        lead = Lead.objects.select_for_update().filter(pk=booking.lead_id, organization_id=booking.organization_id).first()
        if lead is None:
            return
        active = CalendarBooking.objects.filter(
            lead=lead, organization_id=lead.organization_id, status__in=ACTIVE,
        ).order_by("-updated_at").first()
        values = dict(lead.attributes or {})
        values["booked_at"] = (
            active.start_at.astimezone(_page_zone(active.page)).strftime("%Y-%m-%dT%H:%M")
            if active else ""
        )
        Lead.objects.filter(pk=lead.pk, organization_id=lead.organization_id).update(attributes=values)


def enqueue_booking_sync(booking_id):
    from .tasks import sync_booking_calendar
    try:
        sync_booking_calendar.delay(str(booking_id))
    except Exception:
        # Pending state is durable; Beat recovers failed broker submissions.
        from .service_common import logger
        logger.exception("Unable to enqueue calendar sync for booking %s", booking_id)


def sync_manual_booking(lead, value):
    """Called inside Lead.save's transaction; invalid reservations roll it back."""
    if not value:
        if CalendarBooking.objects.filter(lead=lead, organization_id=lead.organization_id, status__in=ACTIVE).exists():
            raise ValidationError({"booked_at": "Cancel the appointment in SHVYA Calendar before clearing Booked at."})
        return
    bookings = list(CalendarBooking.objects.filter(
        lead=lead, organization_id=lead.organization_id, status__in=ACTIVE,
    ).select_related("page")[:2])
    if len(bookings) > 1:
        raise ValidationError({"booked_at": "This lead has multiple appointments. Choose the booking in SHVYA Calendar to reschedule."})
    booking = bookings[0] if bookings else None
    if booking:
        page = booking.page
    else:
        pages = list(CalendarPage.objects.filter(
            organization_id=lead.organization_id, pipeline_id=lead.pipeline_id,
            status=CalendarPage.Status.PUBLISHED,
        ).exclude(page_type=CalendarPage.PageType.LEAD)[:2])
        if len(pages) != 1:
            raise ValidationError({"booked_at": "Use a single published booking calendar for this pipeline, or create the booking in SHVYA Calendar first."})
        page = pages[0]
    page = CalendarPage.objects.select_for_update().get(pk=page.pk, organization_id=lead.organization_id)
    try:
        requested = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timezone.is_naive(requested):
            requested = requested.replace(tzinfo=_page_zone(page))
        requested = requested.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"booked_at": "Enter a valid booking date and time."}) from exc
    if booking and requested == booking.start_at:
        return
    starts = {slot["start"] for slot in available_slots(
        page=page, local_date=requested.astimezone(_page_zone(page)).date(),
        exclude_booking_id=booking.pk if booking else None,
        check_google=True,
    )}
    if requested not in starts:
        raise ValidationError({"booked_at": "This time is outside availability or already booked. Choose an available calendar slot."})
    if booking:
        booking.previous_start_at, booking.previous_end_at = booking.start_at, booking.end_at
        booking.start_at = requested
        booking.end_at = requested + timedelta(minutes=page.slot_duration_minutes)
        booking.status = CalendarBooking.Status.RESCHEDULED
        booking.calendar_sync_status = CalendarBooking.SyncStatus.PENDING
        booking.save(update_fields=["previous_start_at", "previous_end_at", "start_at", "end_at", "status", "calendar_sync_status", "updated_at"])
    else:
        from .booking_services import _create_booking_row
        from .page_services import latest_published_version
        version = latest_published_version(page)
        if not version:
            raise ValidationError({"booked_at": "Publish this calendar before booking."})
        submission = CalendarSubmission.objects.create(
            organization_id=lead.organization_id, page=page, page_version=version,
            lead=lead, status=CalendarSubmission.Status.LEAD_MATCHED,
            attribution={"source": "crm_booked_at"},
        )
        booking, _ = _create_booking_row(page=page, submission=submission, slot_start=requested)
    lead.attributes["booked_at"] = requested.astimezone(_page_zone(page)).strftime("%Y-%m-%dT%H:%M")
    transaction.on_commit(lambda: enqueue_booking_sync(booking.pk))
    from .reminder_services import schedule_booking_reminders
    schedule_booking_reminders(booking)
