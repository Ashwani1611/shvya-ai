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


def page_snapshot(page):
    return {
        "name": page.name,
        "slug": page.slug,
        "page_type": page.page_type,
        "timezone": page.timezone,
        "language": page.language,
        "accent_color": page.accent_color,
        "logo_url": page.logo_display_url,
        "intro_title": page.intro_title,
        "intro_description": page.intro_description,
        "intro_highlights": page.intro_highlights,
        "form_schema": page.form_schema,
        "submit_button_text": page.submit_button_text,
        "consent_enabled": page.consent_enabled,
        "consent_text": page.consent_text,
        "pipeline_id": str(page.pipeline_id or ""),
        "stage_id": str(page.stage_id or ""),
        "host_id": str(page.host_id or ""),
        "duplicate_behavior": page.duplicate_behavior,
        "duplicate_match_email": page.duplicate_match_email,
        "lead_name_prefix": page.lead_name_prefix,
        "attribute_update_policy": page.attribute_update_policy,
        "session_title": page.session_title,
        "session_description": page.session_description,
        "discussion_points": page.discussion_points,
        "availability": page.availability,
        "bookable_days": page.bookable_days,
        "minimum_notice_minutes": page.minimum_notice_minutes,
        "slot_duration_minutes": page.slot_duration_minutes,
        "max_slots_per_day": page.max_slots_per_day,
        "bookings_per_slot": page.bookings_per_slot,
        "buffer_before_minutes": page.buffer_before_minutes,
        "buffer_after_minutes": page.buffer_after_minutes,
        "meeting_location": page.meeting_location,
        "custom_meeting_link": page.custom_meeting_link,
        "invite_lead_to_event": page.invite_lead_to_event,
        "confirmation_heading": page.confirmation_heading,
        "confirmation_message": page.confirmation_message,
        "show_booking_details": page.show_booking_details,
        "show_add_calendar": page.show_add_calendar,
        "redirect_enabled": page.redirect_enabled,
        "redirect_button_text": page.redirect_button_text,
        "redirect_url": page.redirect_url,
        "acknowledgement_enabled": page.acknowledgement_enabled,
        "acknowledgement_subject": page.acknowledgement_subject,
        "acknowledgement_body": page.acknowledgement_body,
    }


def _validate_page_for_publish(page):
    errors = {}

    if not page.pipeline_id:
        errors["pipeline"] = "Select a CRM pipeline before publishing."
    elif not Pipeline.objects.filter(
        id=page.pipeline_id,
        organization_id=page.organization_id,
        is_active=True,
    ).exists():
        errors["pipeline"] = (
            "The selected CRM pipeline is unavailable. Choose an active pipeline "
            "and save Lead Form before publishing."
        )

    if not page.stage_id:
        errors["stage"] = "Select an initial stage before publishing."
    elif not Stage.objects.filter(
        id=page.stage_id,
        pipeline_id=page.pipeline_id,
        is_active=True,
    ).exists():
        errors["stage"] = (
            "The selected stage is unavailable for this pipeline. Choose an active "
            "stage and save Lead Form before publishing."
        )

    if page.page_type != CalendarPage.PageType.LEAD:
        if not page.host_id:
            errors["host"] = "Select a booking host before publishing."
        elif not User.objects.filter(
            id=page.host_id,
            organization_id=page.organization_id,
            is_active=True,
        ).exists():
            errors["host"] = (
                "The selected booking host is no longer active in this organization."
            )

    if (
        page.meeting_location == CalendarPage.MeetingLocation.CUSTOM
        and not page.custom_meeting_link
    ):
        errors["custom_meeting_link"] = "Add the custom meeting link before publishing."

    try:
        ZoneInfo(str(page.timezone or ""))
    except (ZoneInfoNotFoundError, ValueError):
        errors["timezone"] = (
            "Choose a valid booking timezone, for example Asia/Kolkata."
        )

    availability = page.availability or {}
    if not isinstance(availability, dict):
        errors["availability"] = "Availability must be a weekday configuration."
    else:
        for day, rule in availability.items():
            if not isinstance(rule, dict) or not rule.get("enabled"):
                continue
            try:
                start = datetime.strptime(
                    str(rule.get("start") or ""),
                    "%H:%M",
                ).time()
                end = datetime.strptime(
                    str(rule.get("end") or ""),
                    "%H:%M",
                ).time()
            except (TypeError, ValueError):
                errors["availability"] = (
                    f"{str(day).title()} has an invalid working time."
                )
                break
            if start >= end:
                errors["availability"] = (
                    f"{str(day).title()} working hours must end after they start."
                )
                break

    try:
        bookings_per_slot = int(page.bookings_per_slot)
    except (TypeError, ValueError):
        bookings_per_slot = 0
    if bookings_per_slot < 1:
        errors["bookings_per_slot"] = "Bookings per slot must be at least 1."

    try:
        slot_duration = int(page.slot_duration_minutes)
    except (TypeError, ValueError):
        slot_duration = 0
    if slot_duration < 5:
        errors["slot_duration_minutes"] = "Slot duration must be at least 5 minutes."

    if errors:
        raise ValidationError(errors)


def _json_safe_snapshot(page):
    snapshot = page_snapshot(page)
    try:
        # Canonicalize through JSON so the database only receives primitives
        # even if an old row contains a lazy/custom value from an earlier build.
        return json.loads(json.dumps(snapshot))
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "This Calendar page contains invalid saved form data. Save Lead Form "
            "and Scheduling once, then publish again."
        ) from exc


@transaction.atomic
def publish_page(*, page, actor):
    locked = (
        CalendarPage.objects
        .select_for_update()
        .get(pk=page.pk, organization_id=page.organization_id)
    )

    _validate_page_for_publish(locked)
    snapshot = _json_safe_snapshot(locked)

    publisher_id = None
    actor_id = getattr(actor, "pk", None)
    if actor_id and User.objects.filter(
        pk=actor_id,
        organization_id=locked.organization_id,
        is_active=True,
    ).exists():
        publisher_id = actor_id

    published = None
    version = None

    # The locked CalendarPage row serializes normal publications. The retry
    # loop additionally repairs stale current_version values left by older
    # releases and protects against a legacy duplicate version row.
    for _attempt in range(5):
        latest_version = (
            CalendarPageVersion.objects
            .filter(page_id=locked.pk)
            .aggregate(max_version=Max("version"))
            .get("max_version")
            or 0
        )
        version = max(int(locked.current_version or 0), int(latest_version)) + 1
        try:
            with transaction.atomic():
                published = CalendarPageVersion.objects.create(
                    page_id=locked.pk,
                    version=version,
                    snapshot=snapshot,
                    published_by_id=publisher_id,
                )
            break
        except IntegrityError:
            locked.refresh_from_db(fields=["current_version"])

    if published is None or version is None:
        raise ValidationError(
            "SHVYA could not reserve a publication version for this page. "
            "Please save the page once and try again."
        )

    now = timezone.now()
    update_values = {
        "current_version": version,
        "status": CalendarPage.Status.PUBLISHED,
        "published_at": now,
        "updated_at": now,
    }
    if publisher_id:
        update_values["updated_by_id"] = publisher_id

    CalendarPage.objects.filter(
        pk=locked.pk,
        organization_id=locked.organization_id,
    ).update(**update_values)

    locked.current_version = version
    locked.status = CalendarPage.Status.PUBLISHED
    locked.published_at = now
    if publisher_id:
        locked.updated_by_id = publisher_id

    return published

def latest_published_version(page):
    return page.published_versions.order_by("-version").first()
