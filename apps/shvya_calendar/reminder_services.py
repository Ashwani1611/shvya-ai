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


def render_booking_text(text, booking):
    lead = booking.lead
    page = booking.page
    local_start = booking.start_at.astimezone(_page_zone(page))
    values = {
        "lead.name": lead.name,
        "lead.first_name": (lead.name.split()[0] if lead.name else ""),
        "lead.phone": lead.phone,
        "lead.email": lead.email,
        "booking.date": local_start.strftime("%d %b %Y"),
        "booking.start_time": local_start.strftime("%I:%M %p").lstrip("0"),
        "booking.timezone": booking.timezone,
        "booking.duration": f"{page.slot_duration_minutes} minutes",
        "booking.meeting_link": booking.meeting_link,
        "organization.name": page.organization.name,
        "booking.owner.name": getattr(booking.host, "name", "") or "",
    }

    def replace(match):
        token = match.group(1)
        if token.startswith("crm."):
            return str((lead.attributes or {}).get(token[4:], ""))
        return str(values.get(token, match.group(0)))

    return TOKEN_RE.sub(replace, str(text or ""))


def schedule_booking_reminders(booking):
    try:
        sequence = booking.page.reminder_sequence
    except CalendarReminderSequence.DoesNotExist:
        return []

    from .tasks import dispatch_calendar_reminder

    deliveries = []
    for step in sequence.steps.filter(enabled=True):
        if step.timing_mode == step.TimingMode.IMMEDIATE:
            due_at = timezone.now()
        elif (
            step.timing_mode == step.TimingMode.SPECIFIC_TIME
            and step.specific_time is not None
        ):
            zone = _page_zone(booking.page)
            local_date = booking.start_at.astimezone(zone).date()
            local_due = datetime.combine(
                local_date,
                step.specific_time,
                tzinfo=zone,
            )
            due_at = local_due.astimezone(UTC)
        else:
            due_at = booking.start_at + timedelta(
                minutes=step.offset_minutes
            )
        delivery, _created = CalendarReminderDelivery.objects.update_or_create(
            booking=booking,
            step=step,
            defaults={
                "due_at": due_at,
                "status": CalendarReminderDelivery.Status.PENDING,
                "rendered_subject": render_booking_text(step.subject, booking),
                "rendered_body": render_booking_text(step.body, booking),
                "error": "",
                "sent_at": None,
                "completed_at": None,
            },
        )
        deliveries.append(delivery)
        try:
            if due_at <= timezone.now():
                dispatch_calendar_reminder.delay(str(delivery.id))
            else:
                dispatch_calendar_reminder.apply_async(
                    args=[str(delivery.id)],
                    eta=due_at,
                )
        except Exception:
            # The row remains pending and can be safely retried; never lose the booking
            # because the task broker is temporarily unavailable.
            pass
    return deliveries
