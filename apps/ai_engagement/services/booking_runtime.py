from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.services.intent_rules import deterministic_intents, normalize
from apps.ai_engagement.services.intent_types import Intent
from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at
from apps.crm.models.reminder import LeadReminder
from apps.shvya_calendar.availability import available_slots, upcoming_slot_days
from apps.shvya_calendar.booking_services import book_slot
from apps.shvya_calendar.lead_capture_services import notify_submission
from apps.shvya_calendar.models import (
    CalendarBooking,
    CalendarPage,
    CalendarPageVersion,
    CalendarSubmission,
)
from services.crm.reminder_notification_service import (
    reset_reminder_notification_acknowledgements,
)


BOOKING_STATE_KEY = "_shvya_ai_booking"
_ACTIVE_BOOKING_STATUSES = (
    CalendarBooking.Status.SCHEDULED,
    CalendarBooking.Status.RESCHEDULED,
)
_BOOKING_RE = re.compile(
    r"\b(?:book|schedule|arrange|set\s*up)\b.{0,48}\b"
    r"(?:demo|meeting|appointment|consultation|onboarding|call)\b|"
    r"\b(?:demo|meeting|appointment|consultation)\b.{0,48}\b"
    r"(?:book|schedule|slot|time)\b",
    re.IGNORECASE | re.DOTALL,
)
_AVAILABILITY_RE = re.compile(
    r"\b(?:available|availability|free|slot)\b",
    re.IGNORECASE,
)
_RESCHEDULE_RE = re.compile(r"\b(?:reschedule|change|move)\b", re.IGNORECASE)
_CONTINUE_RE = re.compile(
    r"^(?:what\s+next|next|show\s+(?:me\s+)?slots?|slots?|"
    r"other\s+(?:time|times|slot|slots)|more\s+(?:time|times|slot|slots))$",
    re.IGNORECASE,
)
_CONFIRMATIONS = {
    "yes",
    "yes please",
    "confirm",
    "confirmed",
    "book it",
    "book this",
    "do it",
    "go ahead",
    "okay",
    "ok",
    "sure",
}
_SELECTIONS = {
    "1": 0,
    "one": 0,
    "first": 0,
    "first one": 0,
    "2": 1,
    "two": 1,
    "second": 1,
    "second one": 1,
    "3": 2,
    "three": 2,
    "third": 2,
    "third one": 2,
}


@dataclass(frozen=True)
class BookingPlan:
    handled: bool
    mode: str = ""
    page_id: str | None = None
    source_message_id: str = ""
    requested_slot: str | None = None
    offered_slots: tuple[str, ...] = ()
    existing_booking_id: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "mode": self.mode,
            "page_id": self.page_id,
            "source_message_id": self.source_message_id,
            "requested_slot": self.requested_slot,
            "offered_slots": list(self.offered_slots),
            "existing_booking_id": self.existing_booking_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BookingResult:
    handled: bool
    message: str
    status: str
    page_id: str | None = None
    booking_id: str | None = None
    reminder_id: str | None = None
    offered_slots: tuple[str, ...] = ()
    requested_slot: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "status": self.status,
            "page_id": self.page_id,
            "booking_id": self.booking_id,
            "reminder_id": self.reminder_id,
            "offered_slots": list(self.offered_slots),
            "requested_slot": self.requested_slot,
            "metadata": dict(self.metadata),
        }


def booking_capability_available(organization) -> bool:
    if organization is None:
        return False
    return CalendarPage.objects.filter(
        organization_id=organization.pk,
        status=CalendarPage.Status.PUBLISHED,
        page_type__in=[
            CalendarPage.PageType.BOOKING,
            CalendarPage.PageType.LEAD_BOOKING,
        ],
        current_version__gt=0,
    ).exists()


def _state(lead) -> dict[str, Any]:
    attributes = lead.attributes if isinstance(getattr(lead, "attributes", None), dict) else {}
    value = attributes.get(BOOKING_STATE_KEY)
    return dict(value) if isinstance(value, dict) else {}


def _write_state(lead, value: dict[str, Any]) -> None:
    attributes = dict(lead.attributes or {})
    attributes[BOOKING_STATE_KEY] = value
    lead.attributes = attributes
    lead.save(update_fields=["attributes", "updated_at"])


def _selection_index(text: str) -> int | None:
    compact = normalize(text).strip(" .!?;,:\"'")
    if compact in _SELECTIONS:
        return _SELECTIONS[compact]
    match = re.fullmatch(r"(?:option|slot)\s*([123])", compact)
    return int(match.group(1)) - 1 if match else None


def _is_confirmation(text: str) -> bool:
    compact = normalize(text).strip(" .!?;,:\"'")
    return compact in _CONFIRMATIONS


def _is_continuation(text: str) -> bool:
    compact = normalize(text).strip(" .!?;,:\"'")
    return bool(_CONTINUE_RE.fullmatch(compact))


def _page_score(page, *, lead, text: str) -> tuple[int, int, float]:
    haystack = f"{page.name} {page.session_title} {page.session_description}".casefold()
    requested = normalize(text)
    words = ("demo", "onboarding", "consult", "meeting", "appointment", "call")
    relevance = sum(1 for word in words if word in requested and word in haystack)
    pipeline_match = int(bool(lead.pipeline_id and page.pipeline_id == lead.pipeline_id))
    return pipeline_match, relevance, page.updated_at.timestamp()


def _resolve_page(*, organization, lead, text: str, pending: dict[str, Any]):
    pending_page_id = str(pending.get("page_id") or "").strip()
    queryset = (
        CalendarPage.objects
        .filter(
            organization_id=organization.pk,
            status=CalendarPage.Status.PUBLISHED,
            page_type__in=[
                CalendarPage.PageType.BOOKING,
                CalendarPage.PageType.LEAD_BOOKING,
            ],
            current_version__gt=0,
        )
        .select_related("host")
    )
    if pending_page_id:
        page = queryset.filter(pk=pending_page_id).first()
        if page is not None:
            return page
    pages = list(queryset[:25])
    if not pages:
        return None
    pages.sort(key=lambda item: _page_score(item, lead=lead, text=text), reverse=True)
    return pages[0]


def _active_booking(*, organization, lead, page=None):
    queryset = (
        CalendarBooking.objects
        .filter(
            organization_id=organization.pk,
            lead_id=lead.pk,
            status__in=_ACTIVE_BOOKING_STATUSES,
            end_at__gt=timezone.now(),
        )
        .select_related("page", "host")
        .order_by("start_at", "created_at")
    )
    if page is not None:
        queryset = queryset.filter(page_id=page.pk)
    return queryset.first()


def _slot_iso(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value or "").strip()


def _slot_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(parsed):
        return None
    return parsed.astimezone(UTC)


def _format_slot(page, value) -> str:
    start = value if isinstance(value, datetime) else _slot_datetime(str(value))
    if start is None:
        return "the selected time"
    local = start.astimezone(ZoneInfo(page.timezone))
    time_label = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%a, %d %b %Y')} at {time_label} ({page.timezone})"


def _collect_slots(page, *, limit=3) -> tuple[str, ...]:
    results: list[str] = []
    try:
        for day in upcoming_slot_days(page, days=7):
            for item in day.get("slots", []):
                start = item.get("start")
                if isinstance(start, datetime):
                    results.append(_slot_iso(start))
                if len(results) >= limit:
                    return tuple(results)
    except Exception:
        return ()
    return tuple(results)


def _is_available(page, requested_iso: str) -> bool:
    requested = _slot_datetime(requested_iso)
    if requested is None:
        return False
    try:
        local_date = requested.astimezone(ZoneInfo(page.timezone)).date()
        starts = {
            item["start"].astimezone(UTC)
            for item in available_slots(page=page, local_date=local_date)
        }
    except Exception:
        return False
    return requested in starts


def _offer_message(page, offered: tuple[str, ...], *, unavailable=False) -> str:
    if not offered:
        return (
            "I couldn't find an available self-booking slot in the configured "
            "calendar window. I've created a follow-up reminder for our team so "
            "they can arrange the demo with you."
        )
    prefix = (
        "That time isn't available now. Here are the next available demo slots:"
        if unavailable
        else "I can help you book the demo. These slots are currently available:"
    )
    lines = [prefix]
    for index, slot in enumerate(offered, start=1):
        lines.append(f"{index}. {_format_slot(page, slot)}")
    lines.append("Reply with 1, 2 or 3, or send another date and time.")
    return "\n".join(lines)


def _requested_due_at(text: str, *, page) -> str | None:
    return parse_grounded_due_at(text, timezone_name=page.timezone)


def prepare_booking_turn(*, organization, lead, source_message) -> BookingPlan:
    text = str(getattr(source_message, "body", "") or "").strip()
    source_message_id = str(getattr(source_message, "id", "") or "")
    if not text:
        return BookingPlan(False)

    pending = _state(lead)
    pending_status = str(pending.get("status") or "")
    selection = _selection_index(text)
    explicit_booking = (
        Intent.BOOKING_INTENT in deterministic_intents(text)
        or bool(_BOOKING_RE.search(text))
    )
    explicit_availability = (
        Intent.AVAILABILITY_QUESTION in deterministic_intents(text)
        or bool(_AVAILABILITY_RE.search(text))
    )
    pending_continuation = bool(
        pending_status in {"awaiting_selection", "awaiting_confirmation"}
        and (selection is not None or _is_confirmation(text) or _is_continuation(text))
    )

    if not explicit_booking and not explicit_availability and not pending_continuation:
        # A date/time reply to a pending booking is also a valid continuation.
        if pending_status not in {"awaiting_selection", "awaiting_confirmation"}:
            return BookingPlan(False)

    page = _resolve_page(
        organization=organization,
        lead=lead,
        text=text,
        pending=pending,
    )
    if page is None:
        if explicit_booking or explicit_availability or pending_status:
            return BookingPlan(
                True,
                mode="manual",
                source_message_id=source_message_id,
                reason="no_published_booking_page",
            )
        return BookingPlan(False)

    due_at = _requested_due_at(text, page=page)
    if (
        not explicit_booking
        and not explicit_availability
        and not pending_continuation
        and not due_at
    ):
        return BookingPlan(False)

    existing = _active_booking(
        organization=organization,
        lead=lead,
        page=page,
    )
    if existing is not None and not _RESCHEDULE_RE.search(text):
        return BookingPlan(
            True,
            mode="existing",
            page_id=str(page.pk),
            source_message_id=source_message_id,
            existing_booking_id=str(existing.pk),
            reason="active_booking_exists",
        )

    pending_slots = tuple(
        item for item in (pending.get("offered_slots") or [])
        if _slot_datetime(str(item)) is not None
    )
    if pending_status == "awaiting_selection" and selection is not None:
        if selection < len(pending_slots):
            return BookingPlan(
                True,
                mode="book",
                page_id=str(page.pk),
                source_message_id=source_message_id,
                requested_slot=pending_slots[selection],
                reason="selected_offered_slot",
            )

    if pending_status == "awaiting_confirmation" and _is_confirmation(text):
        requested = str(pending.get("requested_slot") or "")
        if _slot_datetime(requested) is not None:
            return BookingPlan(
                True,
                mode="book",
                page_id=str(page.pk),
                source_message_id=source_message_id,
                requested_slot=requested,
                reason="confirmed_available_slot",
            )

    if due_at:
        if _is_available(page, due_at):
            if explicit_availability and not explicit_booking:
                return BookingPlan(
                    True,
                    mode="confirm",
                    page_id=str(page.pk),
                    source_message_id=source_message_id,
                    requested_slot=due_at,
                    reason="requested_slot_available",
                )
            return BookingPlan(
                True,
                mode="book",
                page_id=str(page.pk),
                source_message_id=source_message_id,
                requested_slot=due_at,
                reason="explicit_booking_time_available",
            )
        return BookingPlan(
            True,
            mode="offer",
            page_id=str(page.pk),
            source_message_id=source_message_id,
            offered_slots=_collect_slots(page),
            requested_slot=due_at,
            reason="requested_slot_unavailable",
        )

    offered = _collect_slots(page)
    return BookingPlan(
        True,
        mode="offer",
        page_id=str(page.pk),
        source_message_id=source_message_id,
        offered_slots=offered,
        reason=(
            "booking_slot_selection_required"
            if explicit_booking or pending_status
            else "availability_slots_requested"
        ),
    )


def _staff_reminder_minutes(organization) -> int:
    settings = organization.settings if isinstance(getattr(organization, "settings", None), dict) else {}
    config = settings.get("ai_booking")
    config = config if isinstance(config, dict) else {}
    try:
        value = int(config.get("staff_reminder_minutes_before", 15))
    except (TypeError, ValueError):
        value = 15
    return max(0, min(value, 1440))


def _sync_staff_reminder(*, organization, lead, page, title: str, description: str, due_at):
    assigned_to = page.host if page is not None and getattr(page, "host_id", None) else None
    reminder = (
        LeadReminder._base_manager
        .select_for_update()
        .filter(lead_id=lead.pk)
        .first()
    )
    if reminder is None:
        reminder = LeadReminder.objects.create(
            lead=lead,
            assigned_to=assigned_to,
            title=title[:200],
            description=description,
            due_at=due_at,
            status="pending",
        )
    else:
        reminder.assigned_to = assigned_to
        reminder.title = title[:200]
        reminder.description = description
        reminder.due_at = due_at
        reminder.status = "pending"
        reminder.completed_at = None
        reminder.save(
            update_fields=[
                "assigned_to",
                "title",
                "description",
                "due_at",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
    reset_reminder_notification_acknowledgements(reminder=reminder)
    return reminder


def _submission_for_ai(*, organization, lead, page, state, source_message_id):
    submission_id = str(state.get("submission_id") or "")
    if submission_id:
        existing = CalendarSubmission.objects.filter(
            pk=submission_id,
            organization_id=organization.pk,
            page_id=page.pk,
            lead_id=lead.pk,
        ).first()
        if existing is not None:
            return existing, False

    version = CalendarPageVersion.objects.filter(
        page_id=page.pk,
        version=page.current_version,
    ).first()
    if version is None:
        raise ValidationError("The booking page is not fully published.")

    payload = {
        "name": lead.name,
        "mobile": lead.phone,
    }
    if getattr(lead, "email", ""):
        payload["email"] = lead.email

    submission = CalendarSubmission.objects.create(
        organization_id=organization.pk,
        page=page,
        page_version=version,
        lead=lead,
        status=CalendarSubmission.Status.LEAD_MATCHED,
        submitted_data=payload,
        normalized_data=payload,
        attribution={
            "source": "shvya_ai",
            "source_message_id": source_message_id,
        },
        consent_accepted=True,
        consent_text="Booking requested by the lead in the active conversation.",
        consent_version=version.version,
    )
    transaction.on_commit(
        lambda submission_id=str(submission.pk): notify_submission(submission_id)
    )
    return submission, True


def _booking_confirmation_message(page, booking) -> str:
    message = f"Your demo is booked for {_format_slot(page, booking.start_at)}."
    if booking.meeting_link:
        message += f" Meeting link: {booking.meeting_link}"
    elif page.meeting_location == CalendarPage.MeetingLocation.PHONE:
        message += " Our team will call you at the scheduled time."
    elif page.meeting_location == CalendarPage.MeetingLocation.IN_PERSON:
        message += " This is an in-person appointment."
    message += " I've also scheduled the team reminder for this booking."
    return message


def _manual_result(*, organization, lead, source_message_id: str, reason: str):
    reminder = _sync_staff_reminder(
        organization=organization,
        lead=lead,
        page=None,
        title="Demo booking request · manual scheduling",
        description=(
            "The lead requested a demo, but SHVYA could not complete self-booking. "
            f"Reason: {reason}. Source message: {source_message_id}"
        ),
        due_at=timezone.now(),
    )
    state = {
        "status": "manual_followup",
        "source_message_id": source_message_id,
        "reason": reason,
        "updated_at": timezone.now().isoformat(),
    }
    _write_state(lead, state)
    return BookingResult(
        handled=True,
        status="manual_followup",
        message=(
            "I couldn't complete self-booking from the configured calendar, so "
            "I've created a follow-up reminder for our team to arrange the demo with you."
        ),
        reminder_id=str(reminder.pk),
        metadata={"reason": reason},
    )


def apply_booking_plan(*, organization, lead, source_message, plan: BookingPlan) -> BookingResult:
    if not plan.handled:
        return BookingResult(False, "", "not_handled")

    source_message_id = str(getattr(source_message, "id", "") or plan.source_message_id)
    current = _state(lead)
    if (
        source_message_id
        and str(current.get("last_source_message_id") or "") == source_message_id
        and str(current.get("last_message") or "")
    ):
        return BookingResult(
            True,
            str(current["last_message"]),
            str(current.get("status") or "replayed"),
            page_id=str(current.get("page_id") or "") or None,
            booking_id=str(current.get("booking_id") or "") or None,
            reminder_id=str(current.get("reminder_id") or "") or None,
            offered_slots=tuple(current.get("offered_slots") or ()),
            requested_slot=str(current.get("requested_slot") or "") or None,
            metadata={"replayed": True},
        )

    page = None
    if plan.page_id:
        page = (
            CalendarPage.objects
            .select_related("host")
            .filter(
                pk=plan.page_id,
                organization_id=organization.pk,
                status=CalendarPage.Status.PUBLISHED,
            )
            .first()
        )
    if plan.mode == "manual" or page is None:
        return _manual_result(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            reason=plan.reason or "booking_page_unavailable",
        )

    if plan.mode == "existing":
        booking = CalendarBooking.objects.filter(
            pk=plan.existing_booking_id,
            organization_id=organization.pk,
            lead_id=lead.pk,
            status__in=_ACTIVE_BOOKING_STATUSES,
        ).first()
        if booking is None:
            return _manual_result(
                organization=organization,
                lead=lead,
                source_message_id=source_message_id,
                reason="existing_booking_changed",
            )
        due_at = max(
            timezone.now(),
            booking.start_at - timedelta(minutes=_staff_reminder_minutes(organization)),
        )
        reminder = _sync_staff_reminder(
            organization=organization,
            lead=lead,
            page=page,
            title=f"Demo scheduled · {page.session_title}",
            description=f"Existing booking: {_format_slot(page, booking.start_at)}",
            due_at=due_at,
        )
        message = (
            f"Your demo is already booked for {_format_slot(page, booking.start_at)}."
        )
        if booking.meeting_link:
            message += f" Meeting link: {booking.meeting_link}"
        state = {
            "status": "booked",
            "page_id": str(page.pk),
            "booking_id": str(booking.pk),
            "reminder_id": str(reminder.pk),
            "last_source_message_id": source_message_id,
            "last_message": message,
            "updated_at": timezone.now().isoformat(),
        }
        _write_state(lead, state)
        return BookingResult(
            True,
            message,
            "booked",
            page_id=str(page.pk),
            booking_id=str(booking.pk),
            reminder_id=str(reminder.pk),
        )

    if plan.mode == "confirm" and plan.requested_slot:
        if not _is_available(page, plan.requested_slot):
            refreshed = _collect_slots(page)
            plan = BookingPlan(
                True,
                mode="offer",
                page_id=str(page.pk),
                source_message_id=source_message_id,
                requested_slot=plan.requested_slot,
                offered_slots=refreshed,
                reason="requested_slot_became_unavailable",
            )
        else:
            reminder = _sync_staff_reminder(
                organization=organization,
                lead=lead,
                page=page,
                title="Demo requested · awaiting confirmation",
                description=(
                    "Lead asked whether this slot is available: "
                    f"{_format_slot(page, plan.requested_slot)}"
                ),
                due_at=timezone.now(),
            )
            message = (
                f"Yes — {_format_slot(page, plan.requested_slot)} is currently "
                "available. Reply “confirm” and I'll book it."
            )
            state = {
                "status": "awaiting_confirmation",
                "page_id": str(page.pk),
                "requested_slot": plan.requested_slot,
                "reminder_id": str(reminder.pk),
                "last_source_message_id": source_message_id,
                "last_message": message,
                "updated_at": timezone.now().isoformat(),
            }
            _write_state(lead, state)
            return BookingResult(
                True,
                message,
                "awaiting_confirmation",
                page_id=str(page.pk),
                reminder_id=str(reminder.pk),
                requested_slot=plan.requested_slot,
            )

    if plan.mode == "offer":
        offered = tuple(plan.offered_slots or _collect_slots(page))
        if not offered:
            return _manual_result(
                organization=organization,
                lead=lead,
                source_message_id=source_message_id,
                reason="no_available_slots",
            )
        reminder = _sync_staff_reminder(
            organization=organization,
            lead=lead,
            page=page,
            title="Demo requested · awaiting slot selection",
            description=(
                "The lead requested a demo and SHVYA offered currently available "
                "calendar slots. The reminder stays visible until a slot is confirmed."
            ),
            due_at=timezone.now(),
        )
        message = _offer_message(
            page,
            offered,
            unavailable=plan.reason in {
                "requested_slot_unavailable",
                "requested_slot_became_unavailable",
                "slot_became_unavailable",
            },
        )
        state = {
            "status": "awaiting_selection",
            "page_id": str(page.pk),
            "offered_slots": list(offered),
            "requested_slot": plan.requested_slot,
            "reminder_id": str(reminder.pk),
            "last_source_message_id": source_message_id,
            "last_message": message,
            "updated_at": timezone.now().isoformat(),
        }
        _write_state(lead, state)
        return BookingResult(
            True,
            message,
            "awaiting_selection",
            page_id=str(page.pk),
            reminder_id=str(reminder.pk),
            offered_slots=offered,
            requested_slot=plan.requested_slot,
        )

    if plan.mode != "book" or not plan.requested_slot:
        return _manual_result(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            reason="unsupported_booking_state",
        )

    submission, _created = _submission_for_ai(
        organization=organization,
        lead=lead,
        page=page,
        state=current,
        source_message_id=source_message_id,
    )
    try:
        booking = book_slot(
            page=page,
            submission=submission,
            slot_start_iso=plan.requested_slot,
        )
    except ValidationError:
        offered = _collect_slots(page)
        if not offered:
            return _manual_result(
                organization=organization,
                lead=lead,
                source_message_id=source_message_id,
                reason="selected_slot_unavailable_no_alternatives",
            )
        reminder = _sync_staff_reminder(
            organization=organization,
            lead=lead,
            page=page,
            title="Demo requested · choose another slot",
            description="The selected calendar slot became unavailable; alternatives were offered.",
            due_at=timezone.now(),
        )
        message = _offer_message(page, offered, unavailable=True)
        state = {
            "status": "awaiting_selection",
            "page_id": str(page.pk),
            "submission_id": str(submission.pk),
            "offered_slots": list(offered),
            "reminder_id": str(reminder.pk),
            "last_source_message_id": source_message_id,
            "last_message": message,
            "updated_at": timezone.now().isoformat(),
        }
        _write_state(lead, state)
        return BookingResult(
            True,
            message,
            "awaiting_selection",
            page_id=str(page.pk),
            reminder_id=str(reminder.pk),
            offered_slots=offered,
            requested_slot=plan.requested_slot,
            metadata={"slot_recheck_failed": True},
        )

    due_at = max(
        timezone.now(),
        booking.start_at - timedelta(minutes=_staff_reminder_minutes(organization)),
    )
    reminder = _sync_staff_reminder(
        organization=organization,
        lead=lead,
        page=page,
        title=f"Demo scheduled · {page.session_title}",
        description=(
            f"Booking: {_format_slot(page, booking.start_at)}"
            + (f"\nMeeting link: {booking.meeting_link}" if booking.meeting_link else "")
        ),
        due_at=due_at,
    )
    message = _booking_confirmation_message(page, booking)
    state = {
        "status": "booked",
        "page_id": str(page.pk),
        "submission_id": str(submission.pk),
        "booking_id": str(booking.pk),
        "requested_slot": _slot_iso(booking.start_at),
        "reminder_id": str(reminder.pk),
        "last_source_message_id": source_message_id,
        "last_message": message,
        "updated_at": timezone.now().isoformat(),
    }
    _write_state(lead, state)
    return BookingResult(
        True,
        message,
        "booked",
        page_id=str(page.pk),
        booking_id=str(booking.pk),
        reminder_id=str(reminder.pk),
        requested_slot=_slot_iso(booking.start_at),
        metadata={
            "calendar_sync_status": booking.calendar_sync_status,
            "meeting_link_available": bool(booking.meeting_link),
        },
    )


__all__ = [
    "BOOKING_STATE_KEY",
    "BookingPlan",
    "BookingResult",
    "apply_booking_plan",
    "booking_capability_available",
    "prepare_booking_turn",
]
