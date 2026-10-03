"""Organization-scoped calendar presentation and booking actions."""

from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.crm.authentication import crm_login_required
from apps.crm.models import Pipeline, Stage
from .google import GoogleCalendarError
from .models import CalendarBooking, CalendarPage
from .services import available_slots, move_booking_pipeline, reschedule_booking
from .views import _require_calendar_manager, _validation_text


def _bookings(user):
    _require_calendar_manager(user)
    return CalendarBooking.objects.filter(
        organization=user.organization,
        lead__organization=user.organization,
        page__organization=user.organization,
        submission__organization=user.organization,
    ).select_related(
        "page",
        "lead",
        "lead__pipeline",
        "lead__stage",
        "host",
        "submission__page_version",
    )


def _zone(value):
    try:
        return ZoneInfo(value or "Asia/Kolkata")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Kolkata")


def _safe_link(value):
    try:
        parsed = urlsplit(value or "")
        return (
            value
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc
            else ""
        )
    except ValueError:
        return ""


@crm_login_required
@require_GET
def calendar_workspace(request):
    user = request.crm_user
    _require_calendar_manager(user)
    pipelines = Pipeline.objects.filter(organization=user.organization).order_by("name")
    return render(
        request,
        "shvya_calendar/workspace.html",
        {
            "pipelines": pipelines,
            "calendar_timezone": str(_zone(user.organization.timezone)),
            "calendar_today": timezone.now()
            .astimezone(_zone(user.organization.timezone))
            .date()
            .isoformat(),
        },
    )


@crm_login_required
@require_GET
def calendar_events(request):
    bookings = _bookings(request.crm_user)
    zone = _zone(request.crm_user.organization.timezone)
    try:
        start = date.fromisoformat(request.GET.get("start", ""))
        end = date.fromisoformat(request.GET.get("end", ""))
        if not 0 < (end - start).days <= 42:
            raise ValueError
    except ValueError:
        return JsonResponse(
            {"error": "Choose a date range of 1 to 42 days."}, status=400
        )
    pipeline_id = request.GET.get("pipeline")
    if pipeline_id:
        try:
            pipeline = get_object_or_404(
                Pipeline, pk=pipeline_id, organization=request.crm_user.organization
            )
        except ValidationError:
            return JsonResponse({"error": "Choose a valid pipeline."}, status=400)
        bookings = bookings.filter(lead__pipeline=pipeline)
    bookings = bookings.filter(
        start_at__lt=datetime.combine(end, time.min, zone),
        end_at__gt=datetime.combine(start, time.min, zone),
    ).order_by("start_at", "id")
    # Paginate dense calendars without silently dropping appointments.
    try:
        offset = int(request.GET.get("offset", "0"))
        if offset < 0:
            raise ValueError
    except ValueError:
        return JsonResponse({"error": "Invalid page."}, status=400)
    rows = list(bookings[offset : offset + 501])
    events = [
        {
            "id": str(b.pk),
            "title": f"{b.page.session_title or b.page.name} · {b.lead.name}",
            "start": b.start_at.isoformat(),
            "end": b.end_at.isoformat(),
            "pipeline": b.lead.pipeline.name,
            "pipeline_id": str(b.lead.pipeline_id),
            "status": b.status,
            "detail_url": reverse(
                "shvya_calendar:booking_detail", kwargs={"booking_id": b.pk}
            ),
        }
        for b in rows[:500]
    ]
    return JsonResponse(
        {"events": events, "next_offset": offset + 500 if len(rows) > 500 else None}
    )


@crm_login_required
@require_GET
def booking_detail(request, booking_id):
    booking = get_object_or_404(_bookings(request.crm_user), pk=booking_id)
    snapshot = booking.submission.page_version.snapshot
    kind = snapshot.get("meeting_location", booking.page.meeting_location)
    link = _safe_link(booking.meeting_link)
    if not link and kind == CalendarPage.MeetingLocation.CUSTOM:
        link = _safe_link(snapshot.get("custom_meeting_link", ""))
    fields = {
        f.get("key"): f.get("label", f.get("key"))
        for f in snapshot.get("form_schema", [])
    }
    pipelines = Pipeline.objects.filter(
        organization=request.crm_user.organization, is_active=True
    ).order_by("name")
    stages = Stage.objects.filter(pipeline__in=pipelines, is_active=True).order_by(
        "display_order", "name"
    )
    return render(
        request,
        "shvya_calendar/booking_detail.html",
        {
            "attachments": booking.submission.attachments.all(),
            "booking": booking,
            "booking_zone": str(_zone(booking.timezone)),
            "location": dict(CalendarPage.MeetingLocation.choices).get(
                kind, "No meeting location"
            ),
            "meeting_link": link,
            "is_phone": kind == CalendarPage.MeetingLocation.PHONE,
            "description": snapshot.get("session_description")
            or snapshot.get("intro_description", ""),
            "discussion_points": snapshot.get("discussion_points", []),
            "answers": [
                (fields.get(k, k), v)
                for k, v in booking.submission.submitted_data.items()
            ],
            "pipelines": pipelines,
            "stages": stages,
            "can_reschedule": booking.status
            in [CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED],
            "slot_date": timezone.now()
            .astimezone(_zone(booking.page.timezone))
            .date()
            .isoformat(),
        },
    )


@crm_login_required
@require_GET
def booking_slots(request, booking_id):
    booking = get_object_or_404(_bookings(request.crm_user), pk=booking_id)
    try:
        local_date = date.fromisoformat(request.GET.get("date", ""))
        today = timezone.now().astimezone(_zone(booking.page.timezone)).date()
        if (
            not today
            <= local_date
            <= today + timedelta(days=booking.page.bookable_days)
        ):
            raise ValueError
        slots = available_slots(page=booking.page, local_date=local_date)
    except ValueError:
        return JsonResponse(
            {"error": "Choose a date within the booking window."}, status=400
        )
    except (GoogleCalendarError, ValidationError):
        return JsonResponse(
            {"error": "Availability is temporarily unavailable. Please try again."},
            status=503,
        )
    return JsonResponse(
        {
            "slots": [
                {"value": s["start"].isoformat(), "label": s["label"]} for s in slots
            ],
            "timezone": booking.page.timezone,
        }
    )


@crm_login_required
@require_POST
def booking_update(request, booking_id):
    booking = get_object_or_404(_bookings(request.crm_user), pk=booking_id)
    try:
        action = request.POST.get("booking_action") or request.POST.get("action")
        if action == "reschedule":
            if booking.status not in [
                CalendarBooking.Status.SCHEDULED,
                CalendarBooking.Status.RESCHEDULED,
            ]:
                raise ValidationError("Only active bookings can be rescheduled.")
            reschedule_booking(
                booking=booking, slot_start_iso=request.POST.get("slot_start", "")
            )
        elif action == "move":
            move_booking_pipeline(
                booking=booking,
                actor=request.crm_user,
                pipeline_id=request.POST.get("pipeline"),
                stage_id=request.POST.get("stage"),
            )
        else:
            raise ValidationError("Choose a valid booking action.")
    except ValidationError as exc:
        return JsonResponse({"error": _validation_text(exc)}, status=400)
    except GoogleCalendarError:
        return JsonResponse(
            {"error": "Calendar provider unavailable. Please refresh and try again."},
            status=503,
        )
    return JsonResponse({"ok": True})
