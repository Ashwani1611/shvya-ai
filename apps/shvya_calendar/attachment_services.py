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


def lead_calendar_attachments(lead, *, limit=20):
    return list(
        CalendarSubmissionAttachment.objects
        .filter(
            submission__lead=lead,
            submission__organization=lead.organization,
        )
        .select_related("submission", "submission__page")
        .order_by("-created_at")[:limit]
    )


def attach_calendar_attachments_to_leads(leads, *, organization, limit=20):
    leads = list(leads)
    lead_ids = [lead.id for lead in leads]
    grouped = {lead_id: [] for lead_id in lead_ids}
    counts = {lead_id: 0 for lead_id in lead_ids}
    if lead_ids:
        attachments = (
            CalendarSubmissionAttachment.objects
            .filter(
                submission__lead_id__in=lead_ids,
                submission__organization=organization,
            )
            .select_related("submission", "submission__page")
            .order_by("submission__lead_id", "-created_at")
        )
        for attachment in attachments:
            lead_id = attachment.submission.lead_id
            counts[lead_id] = counts.get(lead_id, 0) + 1
            bucket = grouped.setdefault(lead_id, [])
            if len(bucket) < limit:
                bucket.append(attachment)

    for lead in leads:
        lead.calendar_attachments = grouped.get(lead.id, [])
        lead.calendar_attachment_count = counts.get(lead.id, 0)
    return leads
