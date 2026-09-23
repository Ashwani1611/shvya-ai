# ruff: noqa: F401
import hashlib
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.ratelimit import _client_ip
from apps.accounts.models import User
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.crm.models.lead import normalize_phone
from apps.integrations.services.email import (
    EmailConfigurationError,
    send_organization_email,
)
from services.crm.lead_service import create_lead

from .availability import (
    WEEKDAY_KEYS as WEEKDAY_KEYS,
    _overlaps as _overlaps,
    _page_zone,
    _parse_clock as _parse_clock,
    available_slots,
    upcoming_slot_days as upcoming_slot_days,
)
from .google import (
    GoogleCalendarError,
    create_booking_event,
    update_booking_event,
)
from .models import (
    CalendarBooking,
    CalendarPage,
    CalendarPageVersion,
    CalendarReminderDelivery,
    CalendarReminderSequence,
    CalendarSubmission,
    CalendarSubmissionAttachment,
)


from .service_common import logger
from .reminder_services import schedule_booking_reminders

def _create_booking_row(*, page, submission, slot_start):
    # Lock only the two rows that define the booking contract and operate with
    # scalar FK ids. This avoids lazy/stale related-object dereferences and
    # model-wide validation side effects in the public confirmation path.
    locked_page = (
        CalendarPage.objects
        .select_for_update()
        .only(
            "id",
            "organization_id",
            "host_id",
            "status",
            "slot_duration_minutes",
            "bookings_per_slot",
            "timezone",
            "meeting_location",
            "custom_meeting_link",
        )
        .get(pk=page.pk)
    )
    locked_submission = (
        CalendarSubmission.objects
        .select_for_update()
        .only("id", "page_id", "organization_id", "lead_id")
        .get(
            pk=submission.pk,
            page_id=locked_page.pk,
            organization_id=locked_page.organization_id,
        )
    )

    if locked_page.status != CalendarPage.Status.PUBLISHED:
        raise ValidationError("This booking page is not currently available.")
    if not locked_submission.lead_id:
        raise ValidationError("This lead submission cannot be booked.")

    # Verify the lead still belongs to the same organization without loading a
    # related object that may be stale in a long-lived request.
    if not Lead.objects.filter(
        pk=locked_submission.lead_id,
        organization_id=locked_page.organization_id,
    ).exists():
        raise ValidationError("This lead is no longer available for booking.")

    try:
        duration_minutes = int(locked_page.slot_duration_minutes)
        capacity_limit = int(locked_page.bookings_per_slot)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "This booking page has invalid slot settings. Please contact the team."
        ) from exc
    if duration_minutes < 5 or capacity_limit < 1:
        raise ValidationError(
            "This booking page has invalid slot settings. Please contact the team."
        )

    duration = timedelta(minutes=duration_minutes)
    slot_end = slot_start + duration
    active_statuses = [
        CalendarBooking.Status.SCHEDULED,
        CalendarBooking.Status.RESCHEDULED,
    ]

    # Browser retries/back navigation must never create two appointments for the
    # same lead-form submission.
    existing_for_submission = (
        CalendarBooking.objects
        .filter(
            submission_id=locked_submission.pk,
            status__in=active_statuses,
        )
        .order_by("-created_at")
        .first()
    )
    if existing_for_submission is not None:
        return existing_for_submission, False

    existing = CalendarBooking.objects.filter(
        page_id=locked_page.pk,
        status__in=active_statuses,
        start_at=slot_start,
    ).count()
    if existing >= capacity_limit:
        raise ValidationError("That slot was just booked. Please choose another time.")

    create_values = {
        "organization_id": locked_page.organization_id,
        "page_id": locked_page.pk,
        "submission_id": locked_submission.pk,
        "lead_id": locked_submission.lead_id,
        "host_id": locked_page.host_id,
        "start_at": slot_start,
        "end_at": slot_end,
        "timezone": locked_page.timezone,
        "calendar_sync_status": (
            CalendarBooking.SyncStatus.PENDING
            if locked_page.host_id
            else CalendarBooking.SyncStatus.NOT_CONNECTED
        ),
        # Generate before INSERT instead of relying on model.full_clean()/save
        # ordering. This keeps the public path independent from editable=False
        # unique token validation and legacy blank-token rows.
        "cancel_token": secrets.token_urlsafe(32),
        "reschedule_token": secrets.token_urlsafe(32),
    }
    if locked_page.meeting_location == CalendarPage.MeetingLocation.CUSTOM:
        create_values["meeting_link"] = locked_page.custom_meeting_link

    try:
        with transaction.atomic():
            booking = CalendarBooking.objects.create(**create_values)
    except IntegrityError as exc:
        # If another request raced us, recover the durable appointment for the
        # same submission. Otherwise convert the database error into a normal
        # slot/capacity message rather than the generic public exception toast.
        recovered = (
            CalendarBooking.objects
            .filter(
                submission_id=locked_submission.pk,
                status__in=active_statuses,
            )
            .order_by("-created_at")
            .first()
        )
        if recovered is not None:
            return recovered, False

        if CalendarBooking.objects.filter(
            page_id=locked_page.pk,
            status__in=active_statuses,
            start_at=slot_start,
        ).count() >= capacity_limit:
            raise ValidationError(
                "That slot was just booked. Please choose another time."
            ) from exc
        raise ValidationError(
            "That slot could not be reserved. Please choose another available time."
        ) from exc

    return booking, True


def book_slot(*, page, submission, slot_start_iso):
    if submission.page_id != page.id or not submission.lead_id:
        raise ValidationError("This lead submission cannot be booked.")

    try:
        requested = datetime.fromisoformat(
            str(slot_start_iso).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValidationError("Choose a valid booking time.") from exc
    if timezone.is_naive(requested):
        requested = requested.replace(tzinfo=_page_zone(page))
    requested = requested.astimezone(UTC)

    local_date = requested.astimezone(_page_zone(page)).date()
    valid_starts = {
        item["start"]
        for item in available_slots(page=page, local_date=local_date)
    }
    if requested not in valid_starts:
        raise ValidationError("That slot is no longer available.")

    booking, created = _create_booking_row(
        page=page,
        submission=submission,
        slot_start=requested,
    )
    if not created:
        return booking

    if page.host_id:
        try:
            create_booking_event(booking)
        except Exception as exc:
            # Calendar/Meet sync is a post-booking integration. Once the core
            # booking row exists, a provider/token/network/storage problem must
            # never make the visitor think their appointment failed.
            logger.exception(
                "SHVYA Calendar Google sync failed after booking %s was created",
                booking.id,
            )
            try:
                CalendarBooking.objects.filter(pk=booking.pk).update(
                    calendar_sync_status=CalendarBooking.SyncStatus.FAILED,
                    calendar_sync_error=str(exc)[:1000],
                    updated_at=timezone.now(),
                )
                booking.calendar_sync_status = CalendarBooking.SyncStatus.FAILED
                booking.calendar_sync_error = str(exc)[:1000]
            except Exception:
                logger.exception(
                    "Unable to persist Calendar sync failure for booking %s",
                    booking.id,
                )

    try:
        schedule_booking_reminders(booking)
    except Exception:
        # Reminder creation/delivery is also secondary to the confirmed slot.
        # Keep the appointment durable and surface operational failures in logs
        # rather than returning the visitor to slot selection.
        logger.exception(
            "SHVYA Calendar reminder scheduling failed for booking %s",
            booking.id,
        )

    return booking


def active_booking_statuses():
    return [CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED]


def reschedule_booking(*, booking, slot_start_iso):
    page = booking.page
    try:
        requested = datetime.fromisoformat(
            str(slot_start_iso).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValidationError("Choose a valid booking time.") from exc
    if timezone.is_naive(requested):
        requested = requested.replace(tzinfo=_page_zone(page))
    requested = requested.astimezone(UTC)

    local_date = requested.astimezone(_page_zone(page)).date()
    valid_starts = {
        item["start"]
        for item in available_slots(page=page, local_date=local_date)
    }
    if requested not in valid_starts:
        raise ValidationError("That slot is no longer available.")

    with transaction.atomic():
        locked_page = CalendarPage.objects.select_for_update().get(pk=page.pk)
        locked = (
            CalendarBooking.objects
            .select_for_update(of=("self",))
            .select_related("page", "lead", "submission", "host")
            .get(pk=booking.pk)
        )
        if locked.status not in active_booking_statuses():
            raise ValidationError("Only active bookings can be rescheduled.")
        duration = timedelta(minutes=locked_page.slot_duration_minutes)
        active_statuses = [
            CalendarBooking.Status.SCHEDULED,
            CalendarBooking.Status.RESCHEDULED,
        ]
        capacity = (
            CalendarBooking.objects
            .filter(
                page=locked_page,
                status__in=active_statuses,
                start_at=requested,
            )
            .exclude(pk=locked.pk)
            .count()
        )
        if capacity >= locked_page.bookings_per_slot:
            raise ValidationError(
                "That slot was just booked. Please choose another time."
            )
        locked.previous_start_at = locked.start_at
        locked.previous_end_at = locked.end_at
        locked.start_at = requested
        locked.end_at = requested + duration
        locked.status = CalendarBooking.Status.RESCHEDULED
        locked.calendar_sync_status = (
            CalendarBooking.SyncStatus.PENDING
            if locked.host_id
            else CalendarBooking.SyncStatus.NOT_CONNECTED
        )
        locked.full_clean()
        locked.save(
            update_fields=[
                "previous_start_at",
                "previous_end_at",
                "start_at",
                "end_at",
                "status",
                "calendar_sync_status",
                "updated_at",
            ]
        )

    try:
        update_booking_event(locked)
    except GoogleCalendarError as exc:
        locked.calendar_sync_status = CalendarBooking.SyncStatus.FAILED
        locked.calendar_sync_error = str(exc)
        locked.save(
            update_fields=[
                "calendar_sync_status",
                "calendar_sync_error",
                "updated_at",
            ]
        )
    schedule_booking_reminders(locked)
    return locked


TOKEN_RE = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")
