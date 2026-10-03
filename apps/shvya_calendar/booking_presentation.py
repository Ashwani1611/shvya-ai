"""Read-only, tenant-scoped booking links for CRM surfaces."""
from urllib.parse import urlsplit

from .models import CalendarBooking, CalendarPage


def safe_meeting_link(value):
    try:
        parsed = urlsplit(value or "")
        return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""
    except ValueError:
        return ""


def booking_meeting_link(booking):
    snapshot = booking.submission.page_version.snapshot
    link = safe_meeting_link(booking.meeting_link)
    kind = snapshot.get("meeting_location", booking.page.meeting_location)
    if not link and kind == CalendarPage.MeetingLocation.CUSTOM:
        link = safe_meeting_link(snapshot.get("custom_meeting_link", ""))
    return link


def attach_booking_links_to_leads(leads, *, organization):
    """One query per batch, using the same active booking as booked_at."""
    leads = list(leads)
    for lead in leads:
        lead.calendar_meeting_link = ""
    scoped = {lead.pk: lead for lead in leads if lead.organization_id == organization.pk}
    if not scoped:
        return leads
    bookings = (
        CalendarBooking.objects.filter(
            organization=organization, lead__organization=organization,
            page__organization=organization, submission__organization=organization,
            submission__page_version__page__organization=organization,
            lead_id__in=scoped,
            status__in=[CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED],
        )
        .select_related("page", "submission__page_version")
        .order_by("lead_id", "-updated_at", "-pk")
        .distinct("lead_id")
    )
    for booking in bookings:
        scoped[booking.lead_id].calendar_meeting_link = booking_meeting_link(booking)
    return leads
