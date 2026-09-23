# ruff: noqa: F401
"""Compatibility facade for SHVYA Calendar domain services.

Publishing, lead capture, booking, reminders, attachments, and configuration
live in focused modules. Historical imports and test patch points remain stable.
"""

from . import attachment_services as _attachments
from . import booking_services as _booking
from . import configuration_services as _configuration
from . import lead_capture_services as _lead_capture
from . import page_services as _pages
from . import reminder_services as _reminders
from .availability import (
    WEEKDAY_KEYS as WEEKDAY_KEYS,
    _overlaps as _overlaps,
    _page_zone as _page_zone,
    _parse_clock as _parse_clock,
    available_slots as available_slots,
    upcoming_slot_days as upcoming_slot_days,
)
from .google import (
    create_booking_event as create_booking_event,
    update_booking_event as update_booking_event,
)
from .service_common import (
    ALLOWED_UPLOAD_EXTENSIONS as ALLOWED_UPLOAD_EXTENSIONS,
    ALLOWED_UPLOAD_TYPES as ALLOWED_UPLOAD_TYPES,
    FIELD_KEY_RE as FIELD_KEY_RE,
    MAX_UPLOAD_BYTES as MAX_UPLOAD_BYTES,
    logger as logger,
)


page_snapshot = _pages.page_snapshot
_validate_page_for_publish = _pages._validate_page_for_publish
_json_safe_snapshot = _pages._json_safe_snapshot
latest_published_version = _pages.latest_published_version

hash_ip = _lead_capture.hash_ip
attribution_from_request = _lead_capture.attribution_from_request
_field_schema = _lead_capture._field_schema
_field_value = _lead_capture._field_value
_consent_accepted = _lead_capture._consent_accepted
mapped_lead_values = _lead_capture.mapped_lead_values
_find_existing_lead = _lead_capture._find_existing_lead
_apply_existing_lead_update = _lead_capture._apply_existing_lead_update
notify_submission = _lead_capture.notify_submission

_create_booking_row = _booking._create_booking_row
active_booking_statuses = _booking.active_booking_statuses
render_booking_text = _reminders.render_booking_text
lead_calendar_attachments = _attachments.lead_calendar_attachments
schema_from_json = _configuration.schema_from_json

_IMPLEMENTATIONS = (
    _pages,
    _lead_capture,
    _booking,
    _reminders,
    _attachments,
    _configuration,
)
_ENTRYPOINTS = frozenset(
    {
        "publish_page",
        "validate_public_submission",
        "create_submission_and_lead",
        "book_slot",
        "reschedule_booking",
        "schedule_booking_reminders",
        "attach_calendar_attachments_to_leads",
        "move_booking_pipeline",
    }
)


def _sync_facade_overrides():
    facade = globals()
    for module in _IMPLEMENTATIONS:
        for name in tuple(module.__dict__):
            if name in _ENTRYPOINTS:
                continue
            if name in facade:
                setattr(module, name, facade[name])


def publish_page(*, page, actor):
    _sync_facade_overrides()
    return _pages.publish_page(page=page, actor=actor)


def validate_public_submission(*, page, version, post, files):
    _sync_facade_overrides()
    return _lead_capture.validate_public_submission(
        page=page,
        version=version,
        post=post,
        files=files,
    )


def create_submission_and_lead(
    *,
    page,
    version,
    submitted,
    normalized,
    request,
    files,
):
    _sync_facade_overrides()
    return _lead_capture.create_submission_and_lead(
        page=page,
        version=version,
        submitted=submitted,
        normalized=normalized,
        request=request,
        files=files,
    )


def book_slot(*, page, submission, slot_start_iso):
    _sync_facade_overrides()
    return _booking.book_slot(
        page=page,
        submission=submission,
        slot_start_iso=slot_start_iso,
    )


def reschedule_booking(*, booking, slot_start_iso):
    _sync_facade_overrides()
    return _booking.reschedule_booking(
        booking=booking,
        slot_start_iso=slot_start_iso,
    )


def schedule_booking_reminders(booking):
    _sync_facade_overrides()
    return _reminders.schedule_booking_reminders(booking)


def attach_calendar_attachments_to_leads(leads, *, organization, limit=20):
    _sync_facade_overrides()
    return _attachments.attach_calendar_attachments_to_leads(
        leads,
        organization=organization,
        limit=limit,
    )


def move_booking_pipeline(*, booking, actor, pipeline_id, stage_id):
    _sync_facade_overrides()
    return _configuration.move_booking_pipeline(
        booking=booking,
        actor=actor,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
    )
