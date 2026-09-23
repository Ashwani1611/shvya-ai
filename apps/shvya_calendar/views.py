from datetime import datetime
import logging
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.models import User
from apps.crm.authentication import crm_login_required, get_crm_session
from apps.crm.models import AttributeDefinition, Stage
from apps.crm.views.api import get_user_pipelines

from .google import (
    GoogleCalendarError,
    build_authorize_url,
    exchange_code,
    fetch_userinfo,
    google_is_configured,
    save_connection,
)
from .models import (
    CalendarBlock,
    CalendarBooking,
    CalendarPage,
    CalendarReminderDelivery,
    CalendarReminderSequence,
    CalendarReminderStep,
    CalendarSubmissionAttachment,
    GoogleCalendarConnection,
)

from .services import (
    publish_page,
    schema_from_json,
)

logger = logging.getLogger(__name__)


def _validation_text(exc):
    if hasattr(exc, "message_dict"):
        return " ".join(
            f"{key}: {'; '.join(str(v) for v in values)}"
            for key, values in exc.message_dict.items()
        )
    return "; ".join(getattr(exc, "messages", [str(exc)]))


def _require_calendar_manager(user):
    if user.role != User.Role.ADMIN:
        raise Http404("SHVYA Calendar management is available to organization admins.")


def _page_for_user(user, page_id):
    return get_object_or_404(
        CalendarPage.objects.select_related(
            "organization",
            "pipeline",
            "stage",
            "host",
        ),
        id=page_id,
        organization=user.organization,
    )


def _unique_slug(organization, value, exclude_id=None):
    base = slugify(value)[:100] or "booking"
    candidate = base
    index = 2
    query = CalendarPage.objects.filter(organization=organization)
    if exclude_id:
        query = query.exclude(id=exclude_id)
    while query.filter(slug=candidate).exists():
        candidate = f"{base[:95]}-{index}"
        index += 1
    return candidate


def _int(value, default, minimum=0, maximum=100000):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _checkbox(post, key):
    return str(post.get(key) or "").lower() in {"1", "true", "on", "yes"}


CALENDAR_LOGO_MAX_BYTES = 2 * 1024 * 1024
CALENDAR_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
CALENDAR_LOGO_TYPES = {"image/png", "image/jpeg", "image/webp"}


def _update_page_logo(request, page):
    upload = request.FILES.get("logo_file")
    remove = _checkbox(request.POST, "remove_logo")
    old_name = page.logo_file.name if page.logo_file else ""
    old_storage = page.logo_file.storage if page.logo_file else None

    if remove:
        page.logo_file = ""
        page.logo_url = ""

    if upload is not None:
        extension = Path(str(upload.name or "")).suffix.casefold()
        content_type = str(getattr(upload, "content_type", "") or "").casefold()
        if upload.size > CALENDAR_LOGO_MAX_BYTES:
            raise ValidationError(
                {"logo_file": "Logo must be 2 MB or smaller."}
            )
        if extension not in CALENDAR_LOGO_EXTENSIONS:
            raise ValidationError(
                {"logo_file": "Use PNG, JPG, JPEG or WebP for the logo."}
            )
        if (
            content_type
            and content_type != "application/octet-stream"
            and content_type not in CALENDAR_LOGO_TYPES
        ):
            raise ValidationError(
                {"logo_file": "The uploaded logo file type is not supported."}
            )
        # Store under a server-generated filename/known image extension. The
        # browser and Nginx will serve it strictly as that image media type.
        # Avoid brittle magic-byte checks here: image editors/CDNs can legally
        # add metadata/preambles that caused genuine customer logos to be
        # rejected by the previous implementation.
        page.logo_file = upload

    if old_name and (remove or upload is not None) and old_storage is not None:
        transaction.on_commit(
            lambda storage=old_storage, name=old_name: storage.delete(name)
        )


def _editor_context(request, page, active_tab=None):
    user = request.crm_user
    pipelines = get_user_pipelines(user).filter(
        organization=user.organization,
        is_active=True,
    )
    stages = Stage.objects.filter(
        pipeline__in=pipelines,
        is_active=True,
    ).select_related("pipeline")
    users = User.objects.filter(
        organization=user.organization,
        is_active=True,
    ).order_by("name", "email")
    attributes = AttributeDefinition.objects.filter(is_active=True, 
        organization=user.organization
    ).order_by("display_order", "name")
    connection = None
    if page.host_id:
        connection = GoogleCalendarConnection.objects.filter(
            organization=user.organization,
            user_id=page.host_id,
            is_active=True,
        ).first()
    sequence, _created = CalendarReminderSequence.objects.get_or_create(page=page)
    pending_calls = (
        CalendarReminderDelivery.objects
        .filter(
            booking__organization=user.organization,
            status=CalendarReminderDelivery.Status.ATTENTION,
        )
        .select_related("booking", "booking__lead", "step")
        .order_by("due_at")[:20]
    )
    public_link = request.build_absolute_uri(
        reverse(
            "shvya_calendar_public:page",
            kwargs={"public_id": page.public_id, "slug": page.slug},
        )
    )
    preview_link = request.build_absolute_uri(
        reverse("shvya_calendar:preview", kwargs={"page_id": page.id})
    )
    embed_code = (
        f'<iframe src="{public_link}" title="{page.name}" '
        'style="width:100%;min-height:760px;border:0;border-radius:20px" '
        'loading="lazy"></iframe>'
    )
    return {
        "page": page,
        "public_link": public_link,
        "preview_link": preview_link,
        "embed_code": embed_code,
        "pipelines": pipelines,
        "stages": stages,
        "organization_users": users,
        "attributes": attributes,
        "google_connection": connection,
        "google_configured": google_is_configured(),
        "sequence": sequence,
        "reminder_steps": sequence.steps.all(),
        "pending_call_reminders": pending_calls,
        "active_tab": active_tab or request.GET.get("tab") or "lead",
        "weekday_rows": [
            ("mon", "Mon"),
            ("tue", "Tue"),
            ("wed", "Wed"),
            ("thu", "Thu"),
            ("fri", "Fri"),
            ("sat", "Sat"),
            ("sun", "Sun"),
        ],
    }


@crm_login_required
@require_http_methods(["GET", "POST"])
def calendar_index(request):
    user = request.crm_user
    _require_calendar_manager(user)

    if request.method == "POST":
        name = (request.POST.get("name") or "New Booking Page").strip()[:120]
        org_timezone = user.organization.timezone or "Asia/Kolkata"
        try:
            ZoneInfo(org_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            org_timezone = "Asia/Kolkata"

        try:
            with transaction.atomic():
                page = CalendarPage(
                    organization=user.organization,
                    created_by=user,
                    updated_by=user,
                    host=user,
                    name=name,
                    slug=_unique_slug(user.organization, name),
                    timezone=org_timezone,
                    intro_title=name,
                )
                pipeline = (
                    get_user_pipelines(user)
                    .filter(organization=user.organization, is_active=True)
                    .order_by("name")
                    .first()
                )
                if pipeline:
                    page.pipeline = pipeline
                    page.stage = (
                        Stage.objects
                        .filter(pipeline=pipeline, is_active=True)
                        .order_by("display_order")
                        .first()
                    )
                page.full_clean()
                page.save()
                CalendarReminderSequence.objects.get_or_create(page=page)
            messages.success(request, "SHVYA Calendar page created.")
            return redirect("shvya_calendar:editor", page_id=page.id)
        except ValidationError as exc:
            messages.error(request, _validation_text(exc))
        except Exception:
            logger.exception(
                "Unable to create SHVYA Calendar page",
                extra={"organization_id": str(user.organization_id)},
            )
            messages.error(
                request,
                "The Calendar page could not be created. Please try again.",
            )

    pages = (
        CalendarPage.objects
        .filter(organization=user.organization)
        .annotate(
            submission_count=Count("submissions", distinct=True),
            booking_count=Count(
                "bookings",
                filter=Q(
                    bookings__status__in=[
                        CalendarBooking.Status.SCHEDULED,
                        CalendarBooking.Status.RESCHEDULED,
                        CalendarBooking.Status.COMPLETED,
                    ]
                ),
                distinct=True,
            ),
        )
    )
    upcoming = (
        CalendarBooking.objects
        .filter(
            organization=user.organization,
            status__in=[
                CalendarBooking.Status.SCHEDULED,
                CalendarBooking.Status.RESCHEDULED,
            ],
            start_at__gte=timezone.now(),
        )
        .select_related("lead", "page", "host")
        .order_by("start_at")[:8]
    )
    pending_calls = (
        CalendarReminderDelivery.objects
        .filter(
            booking__organization=user.organization,
            status=CalendarReminderDelivery.Status.ATTENTION,
        )
        .select_related("booking", "booking__lead", "step")
        .order_by("due_at")[:8]
    )
    return render(
        request,
        "shvya_calendar/index.html",
        {
            "pages": pages,
            "upcoming_bookings": upcoming,
            "pending_call_reminders": pending_calls,
        },
    )


@crm_login_required
@require_GET
def calendar_editor(request, page_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    return render(
        request,
        "shvya_calendar/editor.html",
        _editor_context(request, page),
    )


def _save_lead_section(request, page):
    user = request.crm_user
    page.page_type = request.POST.get("page_type") or page.page_type
    page.name = (request.POST.get("name") or page.name).strip()[:120]
    requested_slug = (request.POST.get("slug") or page.slug).strip()
    page.slug = _unique_slug(
        user.organization,
        requested_slug,
        exclude_id=page.id,
    )
    page.intro_title = (request.POST.get("intro_title") or "").strip()[:120]
    page.intro_description = (request.POST.get("intro_description") or "").strip()
    page.intro_highlights = [
        line.strip()[:160]
        for line in (request.POST.get("intro_highlights") or "").splitlines()
        if line.strip()
    ][:8]
    page.logo_url = (request.POST.get("logo_url") or "").strip()
    _update_page_logo(request, page)
    accent = (request.POST.get("accent_color") or "#0060A2").strip()
    page.accent_color = accent if accent.startswith("#") else "#0060A2"
    page.language = (request.POST.get("language") or "en").strip()[:12]
    page.lead_name_prefix = (
        request.POST.get("lead_name_prefix") or ""
    ).strip()[:60]
    page.submit_button_text = (
        request.POST.get("submit_button_text") or "Continue to scheduling"
    ).strip()[:80]
    page.consent_enabled = _checkbox(request.POST, "consent_enabled")
    page.consent_text = (request.POST.get("consent_text") or "").strip()[:300]
    page.duplicate_behavior = (
        request.POST.get("duplicate_behavior")
        or CalendarPage.DuplicateBehavior.USE_EXISTING
    )
    page.duplicate_match_email = _checkbox(request.POST, "duplicate_match_email")
    page.attribute_update_policy = (
        request.POST.get("attribute_update_policy")
        or CalendarPage.AttributeUpdatePolicy.FILL_BLANK
    )
    page.notify_host_on_submission = _checkbox(
        request.POST,
        "notify_host_on_submission",
    )

    allowed_pipelines = get_user_pipelines(user).filter(
        organization=user.organization,
        is_active=True,
    )
    pipeline_id = (request.POST.get("pipeline") or "").strip()
    stage_id = (request.POST.get("stage") or "").strip()
    page.pipeline = (
        get_object_or_404(allowed_pipelines, id=pipeline_id)
        if pipeline_id
        else None
    )
    page.stage = (
        get_object_or_404(
            Stage,
            id=stage_id,
            pipeline=page.pipeline,
            is_active=True,
        )
        if stage_id and page.pipeline
        else None
    )

    host_id = (request.POST.get("host") or "").strip()
    page.host = (
        get_object_or_404(
            User,
            id=host_id,
            organization=user.organization,
            is_active=True,
        )
        if host_id
        else None
    )
    notify_ids = []
    for raw_id in request.POST.getlist("notify_user_ids"):
        if User.objects.filter(
            id=raw_id,
            organization=user.organization,
            is_active=True,
        ).exists():
            notify_ids.append(str(raw_id))
    page.notify_user_ids = notify_ids
    allowed_roles = {User.Role.ADMIN, User.Role.AGENT}
    page.notify_roles = [
        role
        for role in request.POST.getlist("notify_roles")
        if role in allowed_roles
    ]
    page.acknowledgement_enabled = _checkbox(
        request.POST,
        "acknowledgement_enabled",
    )
    page.acknowledgement_subject = (
        request.POST.get("acknowledgement_subject")
        or "We received your request"
    ).strip()[:180]
    page.acknowledgement_body = (
        request.POST.get("acknowledgement_body")
        or "Hi {{lead.name}},\n\nThanks for contacting {{organization.name}}."
    ).strip()
    page.form_schema = schema_from_json(
        organization=user.organization,
        raw=request.POST.get("form_schema"),
    )


def _save_scheduling_section(request, page):
    page.timezone = (request.POST.get("timezone") or page.timezone).strip()[:64]
    page.session_title = (
        request.POST.get("session_title") or page.session_title
    ).strip()[:80]
    page.session_description = (
        request.POST.get("session_description") or ""
    ).strip()[:250]
    page.discussion_points = [
        value.strip()[:160]
        for value in (request.POST.get("discussion_points") or "").splitlines()
        if value.strip()
    ][:12]
    availability = {}
    for key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        availability[key] = {
            "enabled": _checkbox(request.POST, f"{key}_enabled"),
            "start": (request.POST.get(f"{key}_start") or "09:00")[:5],
            "end": (request.POST.get(f"{key}_end") or "18:00")[:5],
        }
    page.availability = availability
    page.bookable_days = _int(
        request.POST.get("bookable_days"),
        30,
        1,
        365,
    )
    page.minimum_notice_minutes = _int(
        request.POST.get("minimum_notice_minutes"),
        60,
        0,
        60 * 24 * 30,
    )
    page.slot_duration_minutes = _int(
        request.POST.get("slot_duration_minutes"),
        30,
        5,
        480,
    )
    page.max_slots_per_day = _int(
        request.POST.get("max_slots_per_day"),
        25,
        1,
        200,
    )
    page.bookings_per_slot = _int(
        request.POST.get("bookings_per_slot"),
        1,
        1,
        100,
    )
    page.buffer_before_minutes = _int(
        request.POST.get("buffer_before_minutes"),
        0,
        0,
        240,
    )
    page.buffer_after_minutes = _int(
        request.POST.get("buffer_after_minutes"),
        0,
        0,
        240,
    )
    page.meeting_location = (
        request.POST.get("meeting_location")
        or CalendarPage.MeetingLocation.GOOGLE_MEET
    )
    page.custom_meeting_link = (
        request.POST.get("custom_meeting_link") or ""
    ).strip()
    page.invite_lead_to_event = _checkbox(
        request.POST,
        "invite_lead_to_event",
    )


def _save_confirmation_section(request, page):
    page.confirmation_heading = (
        request.POST.get("confirmation_heading") or ""
    ).strip()[:80]
    page.confirmation_message = (
        request.POST.get("confirmation_message") or ""
    ).strip()[:250]
    page.show_booking_details = _checkbox(
        request.POST,
        "show_booking_details",
    )
    page.show_add_calendar = _checkbox(
        request.POST,
        "show_add_calendar",
    )
    page.redirect_enabled = _checkbox(request.POST, "redirect_enabled")
    page.redirect_button_text = (
        request.POST.get("redirect_button_text") or ""
    ).strip()[:80]
    page.redirect_url = (request.POST.get("redirect_url") or "").strip()


@crm_login_required
@require_POST
def calendar_editor_save(request, page_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    section = request.POST.get("section") or "lead"

    try:
        with transaction.atomic():
            if section == "lead":
                _save_lead_section(request, page)
            elif section == "scheduling":
                _save_scheduling_section(request, page)
            elif section == "confirmation":
                _save_confirmation_section(request, page)
            elif section == "reminders":
                sequence, _created = CalendarReminderSequence.objects.get_or_create(
                    page=page
                )
                sequence.name = (
                    request.POST.get("sequence_name") or "Calendar Reminders"
                ).strip()[:255]
                sequence.description = (
                    request.POST.get("sequence_description") or ""
                ).strip()[:300]
                sequence.save()
                messages.success(request, "Reminder sequence saved.")
                return redirect(
                    f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}"
                    "?tab=reminders"
                )
            else:
                raise ValidationError("Unsupported calendar settings section.")

            page.updated_by = user
            page.full_clean()
            page.save()

            # Published pages get a fresh immutable snapshot only after the
            # complete settings save succeeds. If publication fails, this
            # transaction rolls back instead of leaving a half-saved page.
            if page.status == CalendarPage.Status.PUBLISHED:
                publish_page(page=page, actor=user)

        messages.success(request, "SHVYA Calendar settings saved.")
    except ValidationError as exc:
        messages.error(request, _validation_text(exc))
    except Exception:
        logger.exception(
            "Unable to save SHVYA Calendar section",
            extra={
                "calendar_page_id": str(page.id),
                "organization_id": str(user.organization_id),
                "section": section,
            },
        )
        messages.error(
            request,
            "We couldn't save these Calendar settings. No partial changes were "
            "published. Please review the fields and try again.",
        )

    tab_map = {
        "lead": "lead",
        "scheduling": "scheduling",
        "confirmation": "confirmation",
        "reminders": "reminders",
    }
    return redirect(
        f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}"
        f"?tab={tab_map.get(section, 'lead')}"
    )


@crm_login_required
@require_POST
def calendar_status(request, page_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    action = request.POST.get("action") or ""

    if action == "publish":
        try:
            publish_page(page=page, actor=user)
        except ValidationError as exc:
            messages.error(request, _validation_text(exc))
            return redirect("shvya_calendar:editor", page_id=page.id)
        except Exception:
            logger.exception(
                "Unable to publish SHVYA Calendar page",
                extra={
                    "calendar_page_id": str(page.id),
                    "organization_id": str(user.organization_id),
                },
            )
            messages.error(
                request,
                (
                    "SHVYA could not publish this page because its saved Calendar "
                    "configuration could not be committed. Re-save Lead Form and "
                    "Scheduling once, then try the status toggle again."
                ),
            )
            return redirect("shvya_calendar:editor", page_id=page.id)

        # Publication is complete at this point. Provider-readiness checks are
        # advisory only and must never turn a successful status change into a
        # misleading failure toast.
        page.refresh_from_db(fields=["status", "meeting_location", "host_id"])
        google_missing = False
        if page.meeting_location == CalendarPage.MeetingLocation.GOOGLE_MEET:
            try:
                google_missing = not GoogleCalendarConnection.objects.filter(
                    organization_id=user.organization_id,
                    user_id=page.host_id,
                    is_active=True,
                ).exists()
            except Exception:
                logger.exception(
                    "Unable to inspect Google Calendar readiness after publication",
                    extra={
                        "calendar_page_id": str(page.id),
                        "organization_id": str(user.organization_id),
                    },
                )

        if google_missing:
            messages.warning(
                request,
                (
                    "Booking page is ON and lead capture is live. Connect the "
                    "booking host's Google Calendar before Google Meet slots can "
                    "be booked."
                ),
            )
        else:
            messages.success(request, "Booking page is ON and published.")
        return redirect("shvya_calendar:editor", page_id=page.id)

    if action == "disable":
        try:
            CalendarPage.objects.filter(
                pk=page.pk,
                organization_id=user.organization_id,
            ).update(
                status=CalendarPage.Status.DISABLED,
                updated_by_id=user.pk,
                updated_at=timezone.now(),
            )
        except Exception:
            logger.exception(
                "Unable to disable SHVYA Calendar page",
                extra={
                    "calendar_page_id": str(page.id),
                    "organization_id": str(user.organization_id),
                },
            )
            messages.error(request, "Booking page could not be disabled. Please try again.")
        else:
            messages.success(request, "Booking page disabled.")
        return redirect("shvya_calendar:editor", page_id=page.id)

    messages.error(request, "Unknown booking page status action.")
    return redirect("shvya_calendar:editor", page_id=page.id)


@crm_login_required
@require_POST
def calendar_reminder_add(request, page_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    sequence, _created = CalendarReminderSequence.objects.get_or_create(page=page)
    channel = request.POST.get("channel") or ""
    allowed = {choice[0] for choice in CalendarReminderStep.Channel.choices}
    if channel not in allowed:
        messages.error(request, "Unsupported reminder type.")
        return redirect(
            f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=reminders"
        )

    timing = request.POST.get("timing") or CalendarReminderStep.TimingMode.IMMEDIATE
    specific_time = None
    if timing == CalendarReminderStep.TimingMode.IMMEDIATE:
        offset = 0
    elif timing == CalendarReminderStep.TimingMode.BEFORE:
        amount = _int(request.POST.get("timing_amount"), 1, 1, 9999)
        unit = request.POST.get("timing_unit") or "hours"
        multiplier = {"minutes": 1, "hours": 60, "days": 1440}.get(unit, 60)
        offset = -(amount * multiplier)
    elif timing == CalendarReminderStep.TimingMode.SPECIFIC_TIME:
        raw_time = (request.POST.get("specific_time") or "").strip()
        try:
            specific_time = datetime.strptime(raw_time, "%H:%M").time()
        except ValueError:
            messages.error(request, "Choose a valid reminder time.")
            return redirect(
                f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}"
                "?tab=reminders"
            )
        offset = 0
    else:
        messages.error(request, "Unsupported reminder timing.")
        return redirect(
            f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}"
            "?tab=reminders"
        )

    next_order = (
        sequence.steps.order_by("-display_order")
        .values_list("display_order", flat=True)
        .first()
        or -1
    ) + 1
    default_names = {
        CalendarReminderStep.Channel.WHATSAPP: "WhatsApp reminder",
        CalendarReminderStep.Channel.EMAIL: "Email reminder",
        CalendarReminderStep.Channel.CALL_REMINDER: "Call reminder",
    }
    CalendarReminderStep.objects.create(
        sequence=sequence,
        channel=channel,
        name=(request.POST.get("name") or default_names[channel]).strip()[:255],
        subject=(request.POST.get("subject") or "").strip()[:180],
        body=(request.POST.get("body") or "").strip(),
        timing_mode=timing,
        offset_minutes=offset,
        specific_time=specific_time,
        display_order=next_order,
    )
    messages.success(request, "Reminder added.")
    return redirect(
        f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=reminders"
    )


@crm_login_required
@require_POST
def calendar_reminder_delete(request, page_id, step_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    step = get_object_or_404(
        CalendarReminderStep,
        id=step_id,
        sequence__page=page,
    )
    if step.deliveries.exists():
        step.enabled = False
        step.save(update_fields=["enabled", "updated_at"])
    else:
        step.delete()
    messages.success(request, "Reminder removed.")
    return redirect(
        f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=reminders"
    )


@crm_login_required
@require_POST
def calendar_block_add(request, page_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    try:
        starts = datetime.fromisoformat(request.POST.get("starts_at") or "")
        ends = datetime.fromisoformat(request.POST.get("ends_at") or "")
        try:
            page_zone = ZoneInfo(page.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError("Choose a valid booking timezone first.") from exc
        if timezone.is_naive(starts):
            starts = timezone.make_aware(starts, page_zone)
        if timezone.is_naive(ends):
            ends = timezone.make_aware(ends, page_zone)
        block = CalendarBlock(
            page=page,
            starts_at=starts,
            ends_at=ends,
            reason=(request.POST.get("reason") or "").strip()[:160],
        )
        block.full_clean()
        block.save()
        messages.success(request, "Blocked time added.")
    except (ValueError, ValidationError) as exc:
        messages.error(request, _validation_text(exc))
    return redirect(
        f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=scheduling"
    )


@crm_login_required
@require_POST
def calendar_block_delete(request, page_id, block_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    get_object_or_404(CalendarBlock, id=block_id, page=page).delete()
    messages.success(request, "Blocked time removed.")
    return redirect(
        f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=scheduling"
    )


@crm_login_required
@require_POST
def call_reminder_complete(request, delivery_id):
    user = request.crm_user
    _require_calendar_manager(user)
    delivery = get_object_or_404(
        CalendarReminderDelivery,
        id=delivery_id,
        booking__organization=user.organization,
        step__channel=CalendarReminderStep.Channel.CALL_REMINDER,
    )
    delivery.status = CalendarReminderDelivery.Status.COMPLETED
    delivery.completed_at = timezone.now()
    delivery.save(
        update_fields=["status", "completed_at", "updated_at"]
    )
    messages.success(request, "Call reminder completed.")
    return redirect(request.POST.get("next") or reverse("shvya_calendar:index"))


@crm_login_required
@require_GET
def calendar_attachment_download(request, attachment_id):
    user = request.crm_user
    attachment = get_object_or_404(
        CalendarSubmissionAttachment.objects.select_related(
            "submission",
            "submission__lead",
            "submission__page",
            "submission__organization",
        ),
        id=attachment_id,
        submission__organization=user.organization,
        submission__lead__isnull=False,
    )
    filename = Path(attachment.original_name or "attachment").name or "attachment"
    try:
        file_handle = attachment.file.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404("Attachment file is no longer available.") from exc
    response = FileResponse(
        file_handle,
        as_attachment=True,
        filename=filename,
        content_type=attachment.content_type or "application/octet-stream",
    )
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@crm_login_required
@require_GET
def calendar_preview(request, page_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    return render(
        request,
        "shvya_calendar/public.html",
        {
            "page": page,
            "config": {
                **{
                    "form_schema": page.form_schema,
                    "intro_title": page.intro_title,
                    "intro_description": page.intro_description,
                    "logo_url": page.logo_display_url,
                    "intro_highlights": page.intro_highlights,
                    "submit_button_text": page.submit_button_text,
                    "consent_enabled": page.consent_enabled,
                    "consent_text": page.consent_text,
                }
            },
            "preview": True,
        },
    )


@crm_login_required
@require_GET
def google_connect(request, page_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    if page.host_id != user.id:
        messages.error(
            request,
            "The selected booking host must connect Google Calendar from their own SHVYA account.",
        )
        return redirect(
            f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=scheduling"
        )

    state = secrets.token_urlsafe(32)
    crm_session = get_crm_session(request)
    crm_session["shvya_calendar_google_state"] = state
    crm_session["shvya_calendar_google_page"] = str(page.id)
    crm_session.save()
    redirect_uri = request.build_absolute_uri(
        reverse("shvya_calendar:google_callback")
    )
    try:
        return redirect(
            build_authorize_url(redirect_uri=redirect_uri, state=state)
        )
    except GoogleCalendarError as exc:
        messages.error(request, str(exc))
        return redirect(
            f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=scheduling"
        )


@crm_login_required
@require_GET
def google_callback(request):
    user = request.crm_user
    _require_calendar_manager(user)
    crm_session = get_crm_session(request)
    expected = crm_session.pop("shvya_calendar_google_state", "")
    page_id = crm_session.pop("shvya_calendar_google_page", "")
    crm_session.save()
    if not page_id:
        messages.error(
            request,
            "Google Calendar connection session expired. Start the connection again.",
        )
        return redirect("shvya_calendar:index")
    try:
        page = _page_for_user(user, page_id)
    except (Http404, ValidationError):
        messages.error(
            request,
            "The Calendar page for this Google connection is no longer available.",
        )
        return redirect("shvya_calendar:index")
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "Google Calendar connection state expired. Try again.")
        return redirect("shvya_calendar:editor", page_id=page.id)
    code = request.GET.get("code")
    if not code:
        messages.error(request, "Google Calendar connection was not completed.")
        return redirect("shvya_calendar:editor", page_id=page.id)

    redirect_uri = request.build_absolute_uri(
        reverse("shvya_calendar:google_callback")
    )
    try:
        token_payload = exchange_code(code=code, redirect_uri=redirect_uri)
        userinfo = fetch_userinfo(token_payload.get("access_token") or "")
        save_connection(
            organization=user.organization,
            user=user,
            token_payload=token_payload,
            userinfo=userinfo,
        )
        messages.success(request, "Google Calendar connected.")
    except (GoogleCalendarError, ValidationError, ValueError, TypeError) as exc:
        messages.error(request, _validation_text(exc))
    except Exception:
        logger.exception(
            "Unexpected Google Calendar OAuth callback failure",
            extra={
                "calendar_page_id": str(page.id),
                "organization_id": str(user.organization_id),
            },
        )
        messages.error(
            request,
            "Google Calendar could not be connected right now. Please try again.",
        )
    return redirect(
        f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=scheduling"
    )


@crm_login_required
@require_POST
def google_disconnect(request, page_id):
    user = request.crm_user
    _require_calendar_manager(user)
    page = _page_for_user(user, page_id)
    if page.host_id != user.id:
        messages.error(
            request,
            "Only the selected booking host can disconnect their Google Calendar.",
        )
        return redirect(
            f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}"
            "?tab=scheduling"
        )

    GoogleCalendarConnection.objects.filter(
        organization=user.organization,
        user=user,
    ).update(is_active=False)

    # Keep published lead-capture pages reachable. Scheduling for Google Meet
    # fails closed in the availability service until this host reconnects.
    affected_pages = CalendarPage.objects.filter(
        organization=user.organization,
        host=user,
        meeting_location=CalendarPage.MeetingLocation.GOOGLE_MEET,
        status=CalendarPage.Status.PUBLISHED,
    ).count()
    if affected_pages:
        messages.warning(
            request,
            (
                "Google Calendar disconnected. Published lead-capture pages "
                "remain live, but Google Meet scheduling is paused until this "
                "host reconnects Calendar."
            ),
        )
    else:
        messages.success(request, "Google Calendar disconnected.")
    return redirect(
        f"{reverse('shvya_calendar:editor', kwargs={'page_id': page.id})}?tab=scheduling"
    )

# Public booking endpoints live in a focused module. Compatibility wrappers
# preserve historical imports, decorator attributes, URL contracts, and test
# patch points from apps.shvya_calendar.views.
from functools import wraps as _compat_wraps  # noqa: E402
from . import public_views as _public_views  # noqa: E402

PUBLIC_FLOW_TOKEN_SALT = _public_views.PUBLIC_FLOW_TOKEN_SALT
PUBLIC_FORM_TOKEN_SALT = _public_views.PUBLIC_FORM_TOKEN_SALT
_booking_flow_token = _public_views._booking_flow_token
_public_form_token = _public_views._public_form_token
_public_page = _public_views._public_page
_rate_limit_public = _public_views._rate_limit_public
_verify_booking_flow_token = _public_views._verify_booking_flow_token
_verify_public_form_token = _public_views._verify_public_form_token

_PUBLIC_VIEW_ENTRYPOINTS = frozenset(
    {
        "public_page",
        "public_submit",
        "public_schedule",
        "public_confirmation",
        "public_reschedule",
        "public_cancel",
    }
)


def _sync_public_view_overrides():
    facade = globals()
    for name in tuple(_public_views.__dict__):
        if name in _PUBLIC_VIEW_ENTRYPOINTS:
            continue
        if name in facade:
            setattr(_public_views, name, facade[name])


def _public_compatibility_wrapper(function):
    @_compat_wraps(function)
    def wrapped(*args, **kwargs):
        _sync_public_view_overrides()
        return function(*args, **kwargs)

    return wrapped


public_page = _public_compatibility_wrapper(_public_views.public_page)
public_submit = _public_compatibility_wrapper(_public_views.public_submit)
public_schedule = _public_compatibility_wrapper(_public_views.public_schedule)
public_confirmation = _public_compatibility_wrapper(_public_views.public_confirmation)
public_reschedule = _public_compatibility_wrapper(_public_views.public_reschedule)
public_cancel = _public_compatibility_wrapper(_public_views.public_cancel)
