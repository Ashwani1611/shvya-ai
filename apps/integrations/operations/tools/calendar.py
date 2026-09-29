"""Tenant-scoped Calendar and booking operations for the Operations MCP."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.db import transaction
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


def _calendar_changes(page, changes):
    if not isinstance(changes, dict) or not changes:
        raise OperationsToolError("changes must be a non-empty object.")
    allowed = {"status", "timezone", "availability", "slot_duration_minutes", "bookable_days", "minimum_notice_minutes", "max_slots_per_day", "bookings_per_slot", "meeting_location"}
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
        if key not in {"status", "timezone", "availability", "meeting_location"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise OperationsToolError(f"{key} must be a positive integer.")
        after[key] = deepcopy(value)
    return after


def upsert_calendar_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_CALENDAR_CONFIG_WRITE, tool_name="upsert_calendar_configuration", arguments=arguments)
    page = _page_for(organization, (arguments or {}).get("page_id"))
    if page is None:
        raise OperationsToolError("Create a Calendar page in the SHVYA dashboard before configuring it through Operations.")
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
        for key in (set(locked_after) & {"status", "timezone", "availability", "slot_duration_minutes", "bookable_days", "minimum_notice_minutes", "max_slots_per_day", "bookings_per_slot", "meeting_location"}):
            if key in (arguments or {}).get("changes", {}):
                setattr(locked, key, (arguments or {})["changes"][key])
        locked.full_clean()
        locked.save()
        result = _page_snapshot(locked)
    return ToolExecution(data={"status": "UPDATED", "page": result, "verification": "passed"}, capability=CAP_CALENDAR_CONFIG_WRITE, target_type="calendar_page", target_id=str(page.id), reason=reason, audit_summary={"verification": "passed"})


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
