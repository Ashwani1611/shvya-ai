"""Tenant-scoped Calendar and booking operations for the Operations MCP."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils.text import slugify
from django.urls import reverse
from django.conf import settings
from apps.accounts.models import User
from apps.crm.models import Pipeline, Stage
from django.utils import timezone

from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_CALENDAR_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    approval_required,
)
from apps.integrations.operations_tools import (
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _require_operations_capability,
    _uuid,
    _write_gate,
)
from apps.shvya_calendar.availability import available_slots
from apps.shvya_calendar.booking_services import reschedule_booking
from apps.shvya_calendar.models import (
    CalendarBooking,
    CalendarPage,
    CalendarReminderDelivery,
    CalendarReminderSequence,
    CalendarReminderStep,
    GoogleCalendarConnection,
)


def _page_for(organization, page_id=None):
    pages = CalendarPage.objects.filter(organization=organization).select_related(
        "pipeline", "stage", "host"
    )
    if page_id:
        page = pages.filter(pk=_uuid(page_id, field="page_id")).first()
        if page is None:
            raise OperationsToolError("Calendar page not found in this organization.")
        return page
    return pages.order_by("name", "id").first()


def _page_snapshot(page):
    if page is None:
        return None
    sequence = CalendarReminderSequence.objects.filter(page=page).first()
    steps = []
    if sequence:
        steps = [
            {
                "id": str(step.id),
                "channel": step.channel,
                "name": step.name,
                "timing_mode": step.timing_mode,
                "offset_minutes": step.offset_minutes,
                "specific_time": step.specific_time.isoformat() if step.specific_time else None,
                "display_order": step.display_order,
                "enabled": step.enabled,
            }
            for step in sequence.steps.order_by("display_order", "created_at")
        ]
    return {
        "id": str(page.id),
        "public_path": reverse("shvya_calendar_public:page", kwargs={"public_id": page.public_id, "slug": page.slug}),
        "public_url": str(getattr(settings, "SUPPORT_PUBLIC_BASE_URL", "") or "https://shvya-ai.com").rstrip("/") + reverse("shvya_calendar_public:page", kwargs={"public_id": page.public_id, "slug": page.slug}),
        "logo_url": page.logo_url,
        "has_uploaded_logo": bool(page.logo_file),
        "accent_color": page.accent_color,
        "intro_title": page.intro_title,
        "intro_description": page.intro_description,
        "session_title": page.session_title,
        "custom_meeting_link": page.custom_meeting_link,
        "name": page.name,
        "slug": page.slug,
        "status": page.status,
        "timezone": page.timezone,
        "page_type": page.page_type,
        "pipeline_id": str(page.pipeline_id) if page.pipeline_id else None,
        "stage_id": str(page.stage_id) if page.stage_id else None,
        "meeting_location": page.meeting_location,
        "slot_duration_minutes": page.slot_duration_minutes,
        "bookable_days": page.bookable_days,
        "minimum_notice_minutes": page.minimum_notice_minutes,
        "max_slots_per_day": page.max_slots_per_day,
        "bookings_per_slot": page.bookings_per_slot,
        "availability": deepcopy(page.availability or {}),
        "reminders": steps,
        "host_id": str(page.host_id) if page.host_id else None,
    }


def _configuration_checks(organization):
    checks = []
    pages = list(CalendarPage.objects.filter(organization=organization).order_by("name", "id"))
    if not pages:
        checks.append({"code": "no_calendar_page", "severity": "warning", "message": "No Calendar page is configured."})
    for page in pages:
        if page.status == CalendarPage.Status.PUBLISHED and not page.pipeline_id:
            checks.append({"code": "published_page_without_pipeline", "severity": "error", "page_id": str(page.id), "message": "Published booking page has no CRM pipeline."})
        if page.status == CalendarPage.Status.PUBLISHED and page.page_type != CalendarPage.PageType.LEAD:
            if not page.stage_id:
                checks.append({"code": "published_booking_without_stage", "severity": "error", "page_id": str(page.id), "message": "Published booking page has no CRM stage."})
        if page.slot_duration_minutes < 5 or page.bookings_per_slot < 1:
            checks.append({"code": "invalid_slot_rules", "severity": "error", "page_id": str(page.id), "message": "Slot duration and capacity must be positive."})
        sequence = CalendarReminderSequence.objects.filter(page=page).first()
        if page.status == CalendarPage.Status.PUBLISHED and sequence is None:
            checks.append({"code": "missing_reminder_sequence", "severity": "warning", "page_id": str(page.id), "message": "Published page has no reminder sequence."})
        elif sequence and not sequence.steps.filter(enabled=True).exists():
            checks.append({"code": "no_enabled_reminders", "severity": "warning", "page_id": str(page.id), "message": "Reminder sequence has no enabled steps."})
    for connection in GoogleCalendarConnection.objects.filter(organization=organization):
        if connection.is_active and not connection.refresh_token_ciphertext and not connection.access_token_ciphertext:
            checks.append({"code": "calendar_connection_without_token", "severity": "error", "connection_id": str(connection.id), "message": "Google Calendar connection has no stored token."})
    return checks


def get_calendar_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    pages = [_page_snapshot(page) for page in CalendarPage.objects.filter(organization=organization).order_by("name", "id")[:100]]
    connections = [
        {
            "id": str(item.id),
            "user_id": str(item.user_id),
            "email": item.email,
            "calendar_id": item.calendar_id,
            "active": item.is_active,
            "has_access_token": bool(item.access_token_ciphertext),
            "has_refresh_token": bool(item.refresh_token_ciphertext),
            "last_error": item.last_error[:500],
        }
        for item in GoogleCalendarConnection.objects.filter(organization=organization).select_related("user")[:100]
    ]
    return ToolExecution(
        data={"pages": pages, "google_calendar_connections": connections, "validation": _configuration_checks(organization), "credentials_returned": False},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
    )


def validate_calendar_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    checks = _configuration_checks(organization)
    return ToolExecution(
        data={"valid": not any(item["severity"] == "error" for item in checks), "checks": checks, "side_effects": False},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
    )



def get_calendar_available_slots(*, identity, arguments):
    """Read real native availability, including booking occupancy and provider busy time."""
    from datetime import date
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    page = _page_for(organization, (arguments or {}).get("page_id"))
    if page is None:
        raise OperationsToolError("No Calendar page exists for this organization.")
    raw_day = (arguments or {}).get("date")
    try:
        day = date.fromisoformat(raw_day)
    except (TypeError, ValueError) as exc:
        raise OperationsToolError("date must use YYYY-MM-DD.") from exc
    try:
        slots = available_slots(page=page, local_date=day)
    except (ValidationError, ValueError) as exc:
        raise OperationsToolError(f"Cannot calculate Calendar availability: {exc}") from exc
    return ToolExecution(
        data={
            "page_id": str(page.id), "date": day.isoformat(), "timezone": page.timezone,
            "page_status": page.status,
            "slots": [{"start": slot["start"].isoformat(), "end": slot["end"].isoformat(),
                       "label": slot["label"]} for slot in slots],
            "slot_count": len(slots),
            "google_calendar_connection_present": GoogleCalendarConnection.objects.filter(
                organization=organization, is_active=True,
            ).exists(),
            "booking_created": False,
        },
        capability=CAP_ORGANIZATION_READ, target_type="calendar_page", target_id=str(page.id),
    )


def get_calendar_setup_readiness(*, identity, arguments):
    """Readiness evidence only; never claim a successful live booking or outbound delivery."""
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    page = _page_for(organization, (arguments or {}).get("page_id"))
    if page is None:
        return ToolExecution(
            data={"ready": False, "page": None, "checks": _configuration_checks(organization),
                  "real_booking_verified": False, "outbound_delivery_verified": False},
            capability=CAP_ORGANIZATION_READ, target_type="organization", target_id=str(organization.id),
        )
    checks = [check for check in _configuration_checks(organization)
              if check.get("page_id") in (None, str(page.id))]
    sequence = CalendarReminderSequence.objects.filter(page=page).first()
    active_reminders = sequence.steps.filter(enabled=True).count() if sequence else 0
    if page.status != CalendarPage.Status.PUBLISHED:
        checks.append({"code": "page_not_published", "severity": "error",
                       "page_id": str(page.id), "message": "Page is not published."})
    return ToolExecution(
        data={
            "ready": not any(check["severity"] == "error" for check in checks),
            "page": _page_snapshot(page),
            "checks": checks,
            "active_reminder_steps": active_reminders,
            "google_calendar_connection_present": GoogleCalendarConnection.objects.filter(
                organization=organization, is_active=True,
            ).exists(),
            "real_booking_verified": False,
            "outbound_delivery_verified": False,
            "verification_level": "configuration_only",
        },
        capability=CAP_ORGANIZATION_READ, target_type="calendar_page", target_id=str(page.id),
    )



def inspect_calendar_public_link(*, identity, arguments):
    """Check published state and Django route resolution without issuing an HTTP request."""
    from django.urls import resolve, Resolver404
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    page = _page_for(organization, (arguments or {}).get("page_id"))
    if page is None:
        raise OperationsToolError("No Calendar page exists for this organization.")
    snapshot = _page_snapshot(page)
    try:
        match = resolve(snapshot["public_path"])
        route_resolves = match.view_name == "shvya_calendar_public:page"
    except Resolver404:
        route_resolves = False
    return ToolExecution(
        data={"page_id": str(page.id), "public_url": snapshot["public_url"],
              "public_path": snapshot["public_path"],
              "published": page.status == CalendarPage.Status.PUBLISHED,
              "route_resolves": route_resolves,
              "public_http_test_performed": False,
              "external_reachability_verified": False,
              "evidence_level": "database_and_local_route"},
        capability=CAP_ORGANIZATION_READ, target_type="calendar_page", target_id=str(page.id),
    )


def get_calendar_delivery_evidence(*, identity, arguments):
    """Return database-backed reminder delivery evidence for one tenant-owned booking."""
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    booking = CalendarBooking.objects.filter(
        pk=_uuid((arguments or {}).get("booking_id"), field="booking_id"),
        organization=organization,
    ).first()
    if booking is None:
        raise OperationsToolError("Booking not found in this organization.")
    deliveries = CalendarReminderDelivery.objects.filter(
        booking=booking,
    ).select_related("step").order_by("due_at", "id")
    evidence = [{
        "delivery_id": str(item.id),
        "channel": item.step.channel,
        "status": item.status,
        "due_at": item.due_at.isoformat(),
        "sent_at": item.sent_at.isoformat() if item.sent_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "error": item.error[:500],
    } for item in deliveries]
    return ToolExecution(
        data={"booking_id": str(booking.id), "booking_status": booking.status,
              "delivery_records": evidence, "delivery_record_count": len(evidence),
              "sent_record_count": sum(item["status"] == CalendarReminderDelivery.Status.SENT for item in evidence),
              "recipient_receipt_verified": False,
              "external_transport_probe_performed": False,
              "evidence_level": "recorded_delivery_state"},
        capability=CAP_ORGANIZATION_READ, target_type="booking", target_id=str(booking.id),
    )


def _calendar_changes(page, changes):
    if not isinstance(changes, dict) or not changes:
        raise OperationsToolError("changes must be a non-empty object.")
    allowed = {"status", "name", "slug", "logo_url", "accent_color", "intro_title", "intro_description", "session_title", "host_id", "pipeline_id", "stage_id", "custom_meeting_link", "timezone", "availability", "slot_duration_minutes", "bookable_days", "minimum_notice_minutes", "max_slots_per_day", "bookings_per_slot", "meeting_location"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise OperationsToolError("Unsupported Calendar fields: " + ", ".join(unknown))
    after = _page_snapshot(page)
    for key, value in changes.items():
        if key == "status" and value not in {choice[0] for choice in CalendarPage.Status.choices}:
            raise OperationsToolError("status is not a supported Calendar page status.")
        if key == "meeting_location" and value not in {choice[0] for choice in CalendarPage.MeetingLocation.choices}:
            raise OperationsToolError("meeting_location is not supported.")
        if key == "timezone" and (not isinstance(value, str) or len(value) > 64):
            raise OperationsToolError("timezone must be a valid IANA timezone name.")
        if key == "availability" and not isinstance(value, dict):
            raise OperationsToolError("availability must be a weekday object.")
        if key in {"name", "slug", "logo_url", "accent_color", "intro_title", "intro_description", "session_title", "custom_meeting_link"}:
            if not isinstance(value, str):
                raise OperationsToolError(f"{key} must be text.")
            if key == "slug":
                if page.status == CalendarPage.Status.PUBLISHED:
                    raise OperationsToolError("Unpublish the page before changing its public slug.")
                if slugify(value) != value or not value or len(value) > 120:
                    raise OperationsToolError("slug must be a valid lowercase slug of at most 120 characters.")
            if key == "name" and (not value.strip() or len(value) > 120):
                raise OperationsToolError("Calendar page name must be 1-120 characters.")
            if key == "logo_url" and value and not value.startswith("https://"):
                raise OperationsToolError("logo_url must be an HTTPS URL.")
        if key in {"host_id", "pipeline_id", "stage_id"}:
            if value is not None:
                value = str(_uuid(value, field=key))
        if key not in {"status", "timezone", "availability", "meeting_location", "name", "slug", "logo_url", "accent_color", "intro_title", "intro_description", "session_title", "custom_meeting_link", "host_id", "pipeline_id", "stage_id"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise OperationsToolError(f"{key} must be a positive integer.")
        after[key] = deepcopy(value)
    return after


def create_calendar_page(*, identity, arguments):
    """Create an unpublished, tenant-owned booking page with an auditable proposal."""
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CALENDAR_CONFIG_WRITE,
        tool_name="create_calendar_page",
        arguments=arguments,
    )
    name = (arguments or {}).get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise OperationsToolError("name must be 1-120 characters.")
    name = name.strip()
    raw_slug = (arguments or {}).get("slug")
    if raw_slug is not None and (not isinstance(raw_slug, str) or not raw_slug.strip()):
        raise OperationsToolError("slug must be a non-empty string.")
    slug = slugify(raw_slug if raw_slug is not None else name)
    if not slug or len(slug) > 120:
        raise OperationsToolError("Provide a valid slug of at most 120 characters.")
    if CalendarPage.objects.filter(organization=organization, slug=slug).exists():
        raise OperationsToolError("A Calendar page with this slug already exists in this organization.")
    page_type = (arguments or {}).get("page_type", CalendarPage.PageType.BOOKING)
    if page_type not in {choice[0] for choice in CalendarPage.PageType.choices}:
        raise OperationsToolError("Unsupported Calendar page type.")
    proposal = {
        "organization_id": str(organization.id),
        "name": name,
        "slug": slug,
        "page_type": page_type,
        "status": CalendarPage.Status.DRAFT,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "page": proposal,
                "approval_required": approval_required(
                    role=identity.role, organization=organization,
                    capability=CAP_CALENDAR_CONFIG_WRITE,
                ),
                "outbound_messages": 0,
            },
            capability=CAP_CALENDAR_CONFIG_WRITE,
            target_type="calendar_page",
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        if CalendarPage.objects.filter(organization=organization, slug=slug).exists():
            raise OperationsToolError("A Calendar page with this slug already exists in this organization.")
        page = CalendarPage(
            organization=organization,
            name=name,
            slug=slug,
            page_type=page_type,
            status=CalendarPage.Status.DRAFT,
        )
        try:
            page.full_clean()
            page.save()
        except ValidationError as exc:
            raise OperationsToolError(f"Invalid Calendar page: {exc}") from exc
    return ToolExecution(
        data={"status": "CREATED", "page": _page_snapshot(page), "verification": "passed", "outbound_messages": 0},
        capability=CAP_CALENDAR_CONFIG_WRITE,
        target_type="calendar_page",
        target_id=str(page.id),
        reason=reason,
        audit_summary={"verification": "passed"},
    )


def upsert_calendar_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_CALENDAR_CONFIG_WRITE, tool_name="upsert_calendar_configuration", arguments=arguments)
    page = _page_for(organization, (arguments or {}).get("page_id"))
    if page is None:
        raise OperationsToolError("No Calendar page exists for this organization. Use create_calendar_page through Operations MCP first, then upsert_calendar_configuration to configure and publish it.")
    after = _calendar_changes(page, (arguments or {}).get("changes"))
    proposal = {"page_id": str(page.id), "before": _page_snapshot(page), "after": after}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(data={"status": "DRY_RUN", "page": after, "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_CALENDAR_CONFIG_WRITE), "reversible": True}, capability=CAP_CALENDAR_CONFIG_WRITE, target_type="calendar_page", target_id=str(page.id), reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN, audit_summary={"proposal_digest": _proposal_digest(proposal)})
    with transaction.atomic():
        locked = CalendarPage.objects.select_for_update().get(pk=page.pk, organization=organization)
        locked_after = _calendar_changes(locked, (arguments or {}).get("changes"))
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal={"page_id": str(locked.id), "before": _page_snapshot(locked), "after": locked_after})
        for key, value in (arguments or {}).get("changes", {}).items():
            if key == "host_id" and value is not None and not User.objects.filter(pk=_uuid(value, field=key), organization=organization, is_active=True).exists():
                raise OperationsToolError("Host must be an active user in this organization.")
            if key == "pipeline_id" and value is not None and not Pipeline.objects.filter(pk=_uuid(value, field=key), organization=organization).exists():
                raise OperationsToolError("Pipeline must belong to this organization.")
            if key == "stage_id" and value is not None and not Stage.objects.filter(pk=_uuid(value, field=key), pipeline__organization=organization).exists():
                raise OperationsToolError("Stage must belong to this organization.")
            setattr(locked, key, value)
        locked.full_clean()
        locked.save()
        result = _page_snapshot(locked)
    return ToolExecution(data={"status": "UPDATED", "page": result, "verification": "passed"}, capability=CAP_CALENDAR_CONFIG_WRITE, target_type="calendar_page", target_id=str(page.id), reason=reason, audit_summary={"verification": "passed"})



def upsert_calendar_reminder(*, identity, arguments):
    """Author a reminder without sending messages or creating bookings."""
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization,
        capability=CAP_CALENDAR_CONFIG_WRITE, tool_name="upsert_calendar_reminder", arguments=arguments)
    page = _page_for(organization, (arguments or {}).get("page_id"))
    if page is None:
        raise OperationsToolError("Calendar page is required.")
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be an object.")
    allowed = {"channel", "name", "subject", "body", "timing_mode", "offset_minutes", "specific_time", "enabled"}
    if set(data) - allowed:
        raise OperationsToolError("Unsupported reminder fields.")
    existing_id = (arguments or {}).get("step_id")
    step = CalendarReminderStep.objects.filter(pk=_uuid(existing_id, field="step_id"), sequence__page=page).first() if existing_id else None
    if existing_id and step is None:
        raise OperationsToolError("Reminder not found on this page.")
    channel = data.get("channel", step.channel if step else None)
    timing = data.get("timing_mode", step.timing_mode if step else "before")
    if channel not in CalendarReminderStep.Channel.values or timing not in CalendarReminderStep.TimingMode.values:
        raise OperationsToolError("Invalid reminder channel or timing_mode.")
    name = data.get("name", step.name if step else "")
    subject = data.get("subject", step.subject if step else "")
    body = data.get("body", step.body if step else "")
    enabled = data.get("enabled", step.enabled if step else True)
    offset = data.get("offset_minutes", step.offset_minutes if step else -120)
    raw_time = data.get("specific_time", step.specific_time.isoformat() if step and step.specific_time else None)
    if not all(isinstance(v, str) for v in (name, subject, body)) or not name.strip() or len(name) > 255 or len(subject) > 180 or len(body) > 10000:
        raise OperationsToolError("Invalid reminder name, subject or body.")
    if not isinstance(enabled, bool) or isinstance(offset, bool) or not isinstance(offset, int):
        raise OperationsToolError("Invalid reminder enabled or offset_minutes.")
    if timing == "before" and offset >= 0:
        raise OperationsToolError("A before-slot reminder must use a negative offset_minutes.")
    try:
        at = datetime.strptime(raw_time, "%H:%M").time() if isinstance(raw_time, str) and raw_time else None
    except ValueError as exc:
        raise OperationsToolError("specific_time must use HH:MM.") from exc
    if timing == "specific_time" and at is None:
        raise OperationsToolError("specific_time is required.")
    before = None if step is None else {"id": str(step.id), "channel": step.channel, "name": step.name, "subject": step.subject, "body": step.body, "timing_mode": step.timing_mode, "offset_minutes": step.offset_minutes, "specific_time": step.specific_time.isoformat() if step.specific_time else None, "enabled": step.enabled}
    after = {"channel": channel, "name": name.strip(), "subject": subject, "body": body, "timing_mode": timing, "offset_minutes": offset, "specific_time": at.isoformat() if at else None, "enabled": enabled}
    proposal = {"page_id": str(page.id), "step_id": str(step.id) if step else None, "before": before, "after": after}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(data={"status": "DRY_RUN", "proposal": proposal,
            "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_CALENDAR_CONFIG_WRITE),
            "messages_sent": 0}, capability=CAP_CALENDAR_CONFIG_WRITE, target_type="calendar_page", target_id=str(page.id),
            reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN, audit_summary={"proposal_digest": _proposal_digest(proposal)})
    with transaction.atomic():
        sequence, _ = CalendarReminderSequence.objects.get_or_create(page=page)
        if step:
            step = CalendarReminderStep.objects.select_for_update().get(pk=step.id, sequence=sequence)
        else:
            order = sequence.steps.order_by("-display_order").values_list("display_order", flat=True).first()
            step = CalendarReminderStep(sequence=sequence, display_order=(order + 1 if order is not None else 0))
        step.channel, step.name, step.subject, step.body = channel, name.strip(), subject, body
        step.timing_mode, step.offset_minutes, step.specific_time, step.enabled = timing, offset, at, enabled
        step.full_clean()
        step.save()
    return ToolExecution(data={"status": "UPDATED" if existing_id else "CREATED", "step_id": str(step.id),
        "verification": "passed", "messages_sent": 0}, capability=CAP_CALENDAR_CONFIG_WRITE,
        target_type="calendar_reminder", target_id=str(step.id), reason=reason, audit_summary={"verification": "passed"})


def verify_booking(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    booking = CalendarBooking.objects.filter(pk=_uuid((arguments or {}).get("booking_id"), field="booking_id"), organization=organization).select_related("page", "lead", "host").first()
    if booking is None:
        raise OperationsToolError("Booking not found in this organization.")
    deliveries = list(booking.reminder_deliveries.select_related("step").order_by("due_at"))
    return ToolExecution(data={"booking": {"id": str(booking.id), "status": booking.status, "start_at": booking.start_at.isoformat(), "end_at": booking.end_at.isoformat(), "timezone": booking.timezone, "page_id": str(booking.page_id), "lead_id": str(booking.lead_id), "calendar_sync_status": booking.calendar_sync_status, "calendar_sync_error": booking.calendar_sync_error[:500]}, "reminders": [{"id": str(item.id), "status": item.status, "due_at": item.due_at.isoformat(), "channel": item.step.channel, "error": item.error[:500]} for item in deliveries], "verified": booking.status in {CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED} and not booking.calendar_sync_error, "credentials_returned": False}, capability=CAP_ORGANIZATION_READ, target_type="booking", target_id=str(booking.id))


def update_booking_status(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_CALENDAR_CONFIG_WRITE, tool_name="update_booking_status", arguments=arguments)
    booking = CalendarBooking.objects.filter(pk=_uuid((arguments or {}).get("booking_id"), field="booking_id"), organization=organization).first()
    if booking is None:
        raise OperationsToolError("Booking not found in this organization.")
    status = str((arguments or {}).get("status") or "")
    allowed = {CalendarBooking.Status.CANCELLED, CalendarBooking.Status.COMPLETED, CalendarBooking.Status.NO_SHOW}
    if status not in allowed:
        raise OperationsToolError("Only cancelled, completed, or no_show status may be set through Operations.")
    proposal = {"booking_id": str(booking.id), "before": booking.status, "after": status}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(data={"status": "DRY_RUN", "before": booking.status, "after": status, "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_CALENDAR_CONFIG_WRITE), "outbound_messages": 0}, capability=CAP_CALENDAR_CONFIG_WRITE, target_type="booking", target_id=str(booking.id), reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN, audit_summary={"proposal_digest": _proposal_digest(proposal)})
    with transaction.atomic():
        locked = CalendarBooking.objects.select_for_update().get(pk=booking.pk, organization=organization)
        if locked.status in {CalendarBooking.Status.CANCELLED, CalendarBooking.Status.COMPLETED, CalendarBooking.Status.NO_SHOW}:
            raise OperationsToolError("Terminal booking status cannot be changed.")
        locked.status = status
        if status == CalendarBooking.Status.CANCELLED:
            locked.cancelled_at = timezone.now()
            CalendarReminderDelivery.objects.filter(booking=locked, status__in=[CalendarReminderDelivery.Status.PENDING, CalendarReminderDelivery.Status.FAILED]).update(status=CalendarReminderDelivery.Status.SKIPPED, error="Booking was cancelled.")
        locked.save(update_fields=["status", "cancelled_at", "updated_at"])
        readback = {"id": str(locked.id), "status": locked.status}
    return ToolExecution(data={"status": "UPDATED", "booking": readback, "verification": "passed", "outbound_messages": 0}, capability=CAP_CALENDAR_CONFIG_WRITE, target_type="booking", target_id=str(booking.id), reason=reason, audit_summary={"verification": "passed"})


def reschedule_booking_operation(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_CALENDAR_CONFIG_WRITE, tool_name="reschedule_booking", arguments=arguments)
    booking = CalendarBooking.objects.filter(pk=_uuid((arguments or {}).get("booking_id"), field="booking_id"), organization=organization).select_related("page").first()
    if booking is None:
        raise OperationsToolError("Booking not found in this organization.")
    slot = str((arguments or {}).get("slot_start") or "")
    try:
        requested = datetime.fromisoformat(slot.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationsToolError("slot_start must be an ISO datetime.") from exc
    if timezone.is_naive(requested):
        try:
            requested = requested.replace(tzinfo=ZoneInfo(booking.page.timezone))
        except Exception as exc:
            raise OperationsToolError("The booking page timezone is invalid.") from exc
    try:
        local_day = requested.astimezone(ZoneInfo(booking.page.timezone)).date()
    except Exception as exc:
        raise OperationsToolError("The booking page timezone is invalid.") from exc
    if not any(item["start"] == requested.astimezone(dt_timezone.utc) for item in available_slots(page=booking.page, local_date=local_day, exclude_booking_id=booking.id)):
        raise OperationsToolError("Requested slot is not currently available.")
    proposal = {"booking_id": str(booking.id), "before": booking.start_at.isoformat(), "after": requested.isoformat()}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(data={"status": "DRY_RUN", "proposal": proposal, "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_CALENDAR_CONFIG_WRITE), "outbound_messages": 0}, capability=CAP_CALENDAR_CONFIG_WRITE, target_type="booking", target_id=str(booking.id), reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN, audit_summary={"proposal_digest": _proposal_digest(proposal)})
    updated = reschedule_booking(booking=booking, slot_start_iso=slot)
    return ToolExecution(data={"status": "UPDATED", "booking": {"id": str(updated.id), "status": updated.status, "start_at": updated.start_at.isoformat(), "end_at": updated.end_at.isoformat(), "calendar_sync_status": updated.calendar_sync_status}, "verification": "passed", "outbound_messages": 0}, capability=CAP_CALENDAR_CONFIG_WRITE, target_type="booking", target_id=str(updated.id), reason=reason, audit_summary={"verification": "passed"})
