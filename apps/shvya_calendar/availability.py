"""Availability calculation for SHVYA Calendar."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.utils import timezone

from .google import free_busy
from .models import CalendarBlock, CalendarBooking, CalendarPage


WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _page_zone(page):
    try:
        return ZoneInfo(page.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError({"timezone": "Choose a valid IANA timezone."}) from exc


def _parse_clock(value):
    try:
        hour, minute = [int(part) for part in str(value).split(":", 1)]
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid availability time.") from exc


def _overlaps(start, end, busy_start, busy_end):
    return start < busy_end and end > busy_start


def available_slots(*, page, local_date, exclude_booking_id=None, check_google=True):
    if page.status != CalendarPage.Status.PUBLISHED:
        return []
    zone = _page_zone(page)
    now = timezone.now()
    local_now = now.astimezone(zone)
    if local_date < local_now.date():
        return []
    if local_date > local_now.date() + timedelta(days=page.bookable_days):
        return []

    rule = (page.availability or {}).get(WEEKDAY_KEYS[local_date.weekday()]) or {}
    if not rule.get("enabled"):
        return []

    start_local = datetime.combine(
        local_date,
        _parse_clock(rule.get("start") or "09:00"),
        tzinfo=zone,
    )
    end_local = datetime.combine(
        local_date,
        _parse_clock(rule.get("end") or "18:00"),
        tzinfo=zone,
    )
    if start_local >= end_local:
        return []

    range_start = start_local.astimezone(UTC)
    range_end = end_local.astimezone(UTC)
    active_statuses = [
        CalendarBooking.Status.SCHEDULED,
        CalendarBooking.Status.RESCHEDULED,
    ]
    booked = list(
        CalendarBooking.objects.filter(
            page=page,
            status__in=active_statuses,
            start_at__lt=range_end,
            end_at__gt=range_start,
        ).exclude(pk=exclude_booking_id).values_list("start_at", "end_at")
    )
    blocked = list(
        CalendarBlock.objects.filter(
            page=page,
            starts_at__lt=range_end,
            ends_at__gt=range_start,
        ).values_list("starts_at", "ends_at")
    )
    google_busy = free_busy(
        page=page,
        time_min=range_start,
        time_max=range_end,
    ) if check_google else []

    duration = timedelta(minutes=page.slot_duration_minutes)
    before = timedelta(minutes=page.buffer_before_minutes)
    after = timedelta(minutes=page.buffer_after_minutes)
    minimum_start = now + timedelta(minutes=page.minimum_notice_minutes)
    results = []
    cursor = start_local
    while cursor + duration <= end_local and len(results) < page.max_slots_per_day:
        slot_start = cursor.astimezone(UTC)
        slot_end = (cursor + duration).astimezone(UTC)
        if slot_start >= minimum_start:
            buffered_start = slot_start - before
            buffered_end = slot_end + after
            hard_busy = [
                (a, b) for a, b in [*blocked, *google_busy]
                if _overlaps(buffered_start, buffered_end, a, b)
            ]
            overlapping_bookings = [
                (a, b) for a, b in booked
                if _overlaps(buffered_start, buffered_end, a, b)
            ]
            if not hard_busy and len(overlapping_bookings) < page.bookings_per_slot:
                results.append(
                    {
                        "start": slot_start,
                        "end": slot_end,
                        "label": cursor.strftime("%I:%M %p").lstrip("0"),
                    }
                )
        cursor += duration
    return results


def upcoming_slot_days(page, *, days=7):
    zone = _page_zone(page)
    start = timezone.now().astimezone(zone).date()
    result = []
    cursor = start
    horizon = start + timedelta(days=page.bookable_days)
    while cursor <= horizon and len(result) < days:
        slots = available_slots(page=page, local_date=cursor)
        if slots:
            result.append({"date": cursor, "slots": slots})
        cursor += timedelta(days=1)
    return result
