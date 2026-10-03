"""Public SHVYA Calendar booking endpoints.

The historical apps.shvya_calendar.views module re-exports these names so URL
contracts and imports remain stable while public booking concerns stay focused.
"""

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.ratelimit import _client_ip

from .google import GoogleCalendarError
from .models import (
    CalendarBooking,
    CalendarPage,
    CalendarSubmission,
)
from .services import (
    attribution_from_request,
    book_slot,
    create_submission_and_lead,
    hash_ip,
    latest_published_version,
    reschedule_booking,
    upcoming_slot_days,
    validate_public_submission,
)


logger = logging.getLogger(__name__)


def _validation_text(exc):
    if hasattr(exc, "message_dict"):
        return " ".join(
            f"{key}: {'; '.join(str(v) for v in values)}"
            for key, values in exc.message_dict.items()
        )
    return "; ".join(getattr(exc, "messages", [str(exc)]))


PUBLIC_FORM_TOKEN_SALT = "shvya-calendar-public-form-v1"
PUBLIC_FLOW_TOKEN_SALT = "shvya-calendar-booking-flow-v1"


def _public_form_token(page, version):
    return signing.dumps(
        {"page": str(page.id), "version": str(version.id)},
        salt=PUBLIC_FORM_TOKEN_SALT,
        compress=True,
    )


def _verify_public_form_token(token, page, version):
    try:
        payload = signing.loads(
            str(token or ""),
            salt=PUBLIC_FORM_TOKEN_SALT,
            max_age=60 * 60 * 24,
        )
    except signing.BadSignature as exc:
        raise ValidationError(
            "This form session expired. Refresh the page and try again."
        ) from exc
    if (
        payload.get("page") != str(page.id)
        or payload.get("version") != str(version.id)
    ):
        raise ValidationError("This form session is no longer valid.")


def _booking_flow_token(page, submission):
    return signing.dumps(
        {"page": str(page.id), "submission": str(submission.id)},
        salt=PUBLIC_FLOW_TOKEN_SALT,
        compress=True,
    )


def _verify_booking_flow_token(token, page, submission):
    try:
        payload = signing.loads(
            str(token or ""),
            salt=PUBLIC_FLOW_TOKEN_SALT,
            max_age=60 * 60 * 24,
        )
    except signing.BadSignature as exc:
        raise ValidationError(
            "This booking session expired. Reopen the booking page."
        ) from exc
    if (
        payload.get("page") != str(page.id)
        or payload.get("submission") != str(submission.id)
    ):
        raise ValidationError("This booking session is no longer valid.")


def _public_page(public_id, slug, *, require_published=True):
    page = get_object_or_404(
        CalendarPage.objects.select_related(
            "organization",
            "pipeline",
            "stage",
            "host",
        ),
        public_id=public_id,
    )
    if require_published and page.status != CalendarPage.Status.PUBLISHED:
        raise Http404
    return page


def _rate_limit_public(request, page):
    ip_key = hash_ip(_client_ip(request))
    if not ip_key:
        return True
    key = f"shvya-calendar:{page.id}:{ip_key}"
    try:
        current = cache.get(key)
        if current is None:
            cache.set(key, 1, timeout=900)
            return True
        if int(current) >= 20:
            return False
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, int(current) + 1, timeout=900)
    except Exception:
        # Lead capture should stay available during a transient cache outage.
        # The signed form token, published-field allowlist and honeypot remain
        # active even when the secondary abuse throttle cannot be reached.
        return True
    return True


@xframe_options_exempt
@require_GET
def public_page(request, public_id, slug):
    page = _public_page(public_id, slug, require_published=False)
    version = latest_published_version(page)
    if page.status != CalendarPage.Status.PUBLISHED or version is None:
        return render(
            request,
            "shvya_calendar/unavailable.html",
            {
                "page": page,
                "is_draft": page.status == CalendarPage.Status.DRAFT,
            },
            status=200,
        )
    return render(
        request,
        "shvya_calendar/public.html",
        {
            "page": page,
            "config": version.snapshot,
            "version": version,
            "preview": False,
            "attribution": attribution_from_request(request),
            "form_token": _public_form_token(page, version),
        },
    )


@csrf_exempt
@xframe_options_exempt
@require_POST
def public_submit(request, public_id, slug):
    page = _public_page(public_id, slug)
    version = latest_published_version(page)
    if version is None:
        raise Http404
    try:
        _verify_public_form_token(
            request.POST.get("form_token"),
            page,
            version,
        )
    except ValidationError as exc:
        return render(
            request,
            "shvya_calendar/public.html",
            {
                "page": page,
                "config": version.snapshot,
                "version": version,
                "preview": False,
                "form_error": _validation_text(exc),
                "form_token": _public_form_token(page, version),
            },
            status=400,
        )

    if not _rate_limit_public(request, page):
        return render(
            request,
            "shvya_calendar/public.html",
            {
                "page": page,
                "config": version.snapshot,
                "version": version,
                "preview": False,
                "form_error": "Too many submissions. Please try again in a few minutes.",
                "form_token": _public_form_token(page, version),
            },
            status=429,
        )

    try:
        submitted, normalized = validate_public_submission(
            page=page,
            version=version,
            post=request.POST,
            files=request.FILES,
        )
        submission, _lead, _created = create_submission_and_lead(
            page=page,
            version=version,
            submitted=submitted,
            normalized=normalized,
            request=request,
            files=request.FILES,
        )
    except ValidationError as exc:
        return render(
            request,
            "shvya_calendar/public.html",
            {
                "page": page,
                "config": version.snapshot,
                "version": version,
                "preview": False,
                "form_error": _validation_text(exc),
                "posted": request.POST,
                "form_token": _public_form_token(page, version),
            },
            status=400,
        )
    except Exception:
        logger.exception(
            "Unexpected SHVYA Calendar public submission failure",
            extra={
                "calendar_page_id": str(page.id),
                "organization_id": str(page.organization_id),
            },
        )
        return render(
            request,
            "shvya_calendar/public.html",
            {
                "page": page,
                "config": version.snapshot,
                "version": version,
                "preview": False,
                "form_error": (
                    "We couldn't process your request right now. "
                    "Please try again in a moment."
                ),
                "posted": request.POST,
                "form_token": _public_form_token(page, version),
            },
            status=503,
        )

    if submission.status == CalendarSubmission.Status.DUPLICATE_BLOCKED:
        return render(
            request,
            "shvya_calendar/lead_success.html",
            {
                "page": page,
                "submission": submission,
                "duplicate_blocked": True,
            },
        )

    if page.page_type == CalendarPage.PageType.LEAD:
        return render(
            request,
            "shvya_calendar/lead_success.html",
            {"page": page, "submission": submission},
        )

    return redirect(
        "shvya_calendar_public:schedule",
        public_id=page.public_id,
        slug=page.slug,
        submission_id=submission.id,
    )


@csrf_exempt
@xframe_options_exempt
@require_http_methods(["GET", "POST"])
def public_schedule(request, public_id, slug, submission_id):
    page = _public_page(public_id, slug)
    submission = get_object_or_404(
        CalendarSubmission.objects.select_related("lead", "page_version"),
        id=submission_id,
        page=page,
        organization=page.organization,
    )
    if not submission.lead_id:
        raise Http404

    error = ""
    flow_token = _booking_flow_token(page, submission)
    if request.method == "POST":
        try:
            _verify_booking_flow_token(
                request.POST.get("flow_token"),
                page,
                submission,
            )
            booking = book_slot(
                page=page,
                submission=submission,
                slot_start_iso=request.POST.get("slot_start") or "",
            )
            return redirect(
                "shvya_calendar_public:confirmation",
                booking_id=booking.id,
                cancel_token=booking.cancel_token,
            )
        except (ValidationError, GoogleCalendarError) as exc:
            error = _validation_text(exc)
        except Exception:
            logger.exception(
                "Unexpected SHVYA Calendar booking failure",
                extra={
                    "calendar_page_id": str(page.id),
                    "submission_id": str(submission.id),
                    "organization_id": str(page.organization_id),
                },
            )

            # A failure can happen after the core booking row has already been
            # committed (for example while syncing Google Calendar or creating
            # reminders). Recover that durable appointment instead of asking
            # the visitor to choose the same slot again.
            recovered = (
                CalendarBooking.objects
                .filter(
                    submission=submission,
                    page=page,
                    organization=page.organization,
                    status__in=[
                        CalendarBooking.Status.SCHEDULED,
                        CalendarBooking.Status.RESCHEDULED,
                    ],
                )
                .order_by("-created_at")
                .first()
            )
            if recovered is not None:
                return redirect(
                    "shvya_calendar_public:confirmation",
                    booking_id=recovered.id,
                    cancel_token=recovered.cancel_token,
                )

            error = (
                "We couldn't confirm that slot right now. "
                "Your lead details are safe; please choose a time again."
            )

    try:
        slot_days = upcoming_slot_days(page)
    except (GoogleCalendarError, ValidationError) as exc:
        slot_days = []
        error = _validation_text(exc)

    return render(
        request,
        "shvya_calendar/schedule.html",
        {
            "page": page,
            "submission": submission,
            "slot_days": slot_days,
            "booking_error": error,
            "flow_token": flow_token,
        },
    )


@xframe_options_exempt
@require_GET
def public_confirmation(request, booking_id, cancel_token):
    booking = get_object_or_404(
        CalendarBooking.objects.select_related(
            "page",
            "organization",
            "lead",
            "host",
        ),
        id=booking_id,
        cancel_token=cancel_token,
    )
    try:
        booking_zone = ZoneInfo(booking.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        booking_zone = timezone.get_current_timezone()
    return render(
        request,
        "shvya_calendar/confirmation.html",
        {
            "booking": booking,
            "page": booking.page,
            "booking_local_start": booking.start_at.astimezone(booking_zone),
            "cancelled": booking.status == CalendarBooking.Status.CANCELLED,
        },
    )


@csrf_exempt
@xframe_options_exempt
@require_http_methods(["GET", "POST"])
def public_reschedule(request, booking_id, reschedule_token):
    booking = get_object_or_404(
        CalendarBooking.objects.select_related(
            "page",
            "organization",
            "lead",
            "submission",
            "host",
        ),
        id=booking_id,
        reschedule_token=reschedule_token,
    )
    page = booking.page
    if booking.status == CalendarBooking.Status.CANCELLED:
        return render(
            request,
            "shvya_calendar/confirmation.html",
            {"booking": booking, "page": page, "cancelled": True},
            status=409,
        )

    error = ""
    if request.method == "POST":
        try:
            booking = reschedule_booking(
                booking=booking,
                slot_start_iso=request.POST.get("slot_start") or "",
            )
            return redirect(
                "shvya_calendar_public:confirmation",
                booking_id=booking.id,
                cancel_token=booking.cancel_token,
            )
        except (ValidationError, GoogleCalendarError) as exc:
            error = _validation_text(exc)
        except Exception:
            logger.exception(
                "Unexpected SHVYA Calendar reschedule failure",
                extra={
                    "booking_id": str(booking.id),
                    "calendar_page_id": str(page.id),
                    "organization_id": str(page.organization_id),
                },
            )
            error = (
                "We couldn't reschedule this booking right now. "
                "Your current booking is unchanged."
            )

    try:
        slot_days = upcoming_slot_days(page)
    except GoogleCalendarError:
        slot_days = []
        error = (
            "Live calendar availability is temporarily unavailable. "
            "Please try again shortly."
        )

    try:
        booking_zone = ZoneInfo(booking.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        booking_zone = timezone.get_current_timezone()
    return render(
        request,
        "shvya_calendar/reschedule.html",
        {
            "booking": booking,
            "page": page,
            "slot_days": slot_days,
            "booking_error": error,
            "booking_local_start": booking.start_at.astimezone(booking_zone),
        },
    )


@csrf_exempt
@xframe_options_exempt
@require_POST
def public_cancel(request, booking_id, cancel_token):
    booking = get_object_or_404(
        CalendarBooking.objects.select_related("page"),
        id=booking_id,
        cancel_token=cancel_token,
    )
    from .cancellation_services import cancel_booking
    cancel_booking(booking)
    return render(
        request,
        "shvya_calendar/confirmation.html",
        {
            "booking": booking,
            "page": booking.page,
            "cancelled": True,
        },
    )


@require_GET
def public_booking_status(request, booking_id, cancel_token):
    from .booking_presentation import safe_meeting_link
    booking = get_object_or_404(CalendarBooking.objects.select_related("page"),
                                pk=booking_id, cancel_token=cancel_token)
    active = booking.status != CalendarBooking.Status.CANCELLED
    response = JsonResponse({
        "status": booking.status,
        "sync_status": booking.calendar_sync_status,
        "event_url": safe_meeting_link(booking.google_event_url) if active and booking.page.show_add_calendar else "",
        "meeting_link": safe_meeting_link(booking.meeting_link) if active else "",
    })
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
