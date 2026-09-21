import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.crm.models import AttributeDefinition, Lead
from apps.crm.models.lead import normalize_phone
from services.crm.lead_service import create_lead

from .google import GoogleCalendarError, create_booking_event, free_busy
from .models import (
    CalendarBlock,
    CalendarBooking,
    CalendarPage,
    CalendarPageVersion,
    CalendarReminderDelivery,
    CalendarReminderSequence,
    CalendarSubmission,
    CalendarSubmissionAttachment,
)


WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ALLOWED_UPLOAD_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def page_snapshot(page):
    return {
        "name": page.name,
        "slug": page.slug,
        "page_type": page.page_type,
        "timezone": page.timezone,
        "accent_color": page.accent_color,
        "logo_url": page.logo_url,
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
    }


@transaction.atomic
def publish_page(*, page, actor):
    page = (
        CalendarPage.objects
        .select_for_update()
        .select_related("pipeline", "stage", "host")
        .get(pk=page.pk)
    )
    if not page.pipeline_id or not page.stage_id:
        raise ValidationError(
            "Select a CRM pipeline and initial stage before publishing."
        )
    page.full_clean()
    if page.page_type != CalendarPage.PageType.LEAD:
        if not page.host_id:
            raise ValidationError("Select a booking host before publishing.")
        if page.meeting_location == CalendarPage.MeetingLocation.GOOGLE_MEET:
            from .google import connection_for_page
            if connection_for_page(page) is None:
                raise ValidationError(
                    "Connect the booking host's Google Calendar before publishing "
                    "a Google Meet page."
                )

    version = page.current_version + 1
    published = CalendarPageVersion.objects.create(
        page=page,
        version=version,
        snapshot=page_snapshot(page),
        published_by=actor,
    )
    page.current_version = version
    page.status = CalendarPage.Status.PUBLISHED
    page.published_at = timezone.now()
    page.updated_by = actor
    page.save(
        update_fields=[
            "current_version",
            "status",
            "published_at",
            "updated_by",
            "updated_at",
        ]
    )
    return published


def latest_published_version(page):
    return page.published_versions.order_by("-version").first()


def hash_ip(ip):
    if not ip:
        return ""
    material = f"{settings.SECRET_KEY}:{ip}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def attribution_from_request(request):
    keys = (
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "gclid",
        "fbclid",
    )
    return {
        key: str(request.GET.get(key) or request.POST.get(key) or "")[:300]
        for key in keys
        if request.GET.get(key) or request.POST.get(key)
    }


def _field_schema(version):
    schema = version.snapshot.get("form_schema") or []
    return [item for item in schema if isinstance(item, dict)]


def _field_value(post, key):
    return str(post.get(f"f_{key}") or "").strip()


def validate_public_submission(*, page, version, post, files):
    errors = {}
    submitted = {}
    normalized = {}

    name = str(post.get("name") or "").strip()
    mobile = str(post.get("mobile") or "").strip()
    if not name:
        errors["name"] = "Name is required."
    if not mobile:
        errors["mobile"] = "Mobile is required."
    else:
        try:
            normalized["mobile"] = normalize_phone(mobile)
        except ValidationError as exc:
            errors["mobile"] = "; ".join(exc.messages)

    submitted["name"] = name
    submitted["mobile"] = mobile
    normalized["name"] = name

    schema = _field_schema(version)
    allowed_keys = set()
    for field in schema:
        key = str(field.get("key") or "").strip()
        if not FIELD_KEY_RE.fullmatch(key):
            continue
        allowed_keys.add(key)
        field_type = str(field.get("field_type") or "text")
        required = bool(field.get("required"))

        if field_type == "file":
            upload = files.get(f"f_{key}")
            if required and upload is None:
                errors[key] = "This file is required."
            if upload is not None:
                if upload.size > MAX_UPLOAD_BYTES:
                    errors[key] = "File must be 10 MB or smaller."
                elif upload.content_type not in ALLOWED_UPLOAD_TYPES:
                    errors[key] = "This file type is not allowed."
                submitted[key] = upload.name
            continue

        value = _field_value(post, key)
        submitted[key] = value
        normalized[key] = value
        if required and not value:
            errors[key] = "This field is required."
            continue
        if not value:
            continue

        if field_type == "email":
            try:
                validate_email(value)
            except ValidationError:
                errors[key] = "Enter a valid email address."
        elif field_type == "numeric":
            try:
                float(value)
            except ValueError:
                errors[key] = "Enter a valid number."
        elif field_type == "option":
            options = [str(v) for v in field.get("options") or []]
            if value not in options:
                errors[key] = "Choose one of the available options."
        elif field_type == "url" and not re.match(r"^https?://", value, re.I):
            errors[key] = "Enter a full http:// or https:// URL."

    for key in post.keys():
        if key.startswith("f_") and key[2:] not in allowed_keys:
            errors[key[2:]] = "This field is not part of the published form."

    if version.snapshot.get("consent_enabled"):
        accepted = str(post.get("consent") or "").lower() in {"1", "true", "on", "yes"}
        if not accepted:
            errors["consent"] = "Please accept the consent statement to continue."

    if str(post.get("website") or "").strip():
        errors["form"] = "Unable to process this submission."

    if errors:
        raise ValidationError(errors)

    return submitted, normalized


def mapped_lead_values(*, organization, version, normalized):
    email = ""
    attributes = {}
    definitions = {
        item.key: item
        for item in AttributeDefinition.objects.filter(organization=organization)
    }
    for field in _field_schema(version):
        key = str(field.get("key") or "")
        value = normalized.get(key)
        if value in (None, ""):
            continue
        map_to = str(field.get("map_to") or "submission")
        if map_to == "email":
            email = str(value)
        elif map_to.startswith("attr:"):
            attribute_key = map_to.split(":", 1)[1]
            definition = definitions.get(attribute_key)
            if definition is None:
                continue
            if definition.field_type == AttributeDefinition.FieldType.OPTION:
                if str(value) not in [str(v) for v in definition.options]:
                    continue
            attributes[attribute_key] = value
    return email, attributes


def _find_existing_lead(*, organization, phone, email, match_email):
    lead = (
        Lead.objects
        .select_for_update()
        .filter(organization=organization, phone=phone)
        .first()
    )
    if lead or not match_email or not email:
        return lead

    email_matches = list(
        Lead.objects
        .select_for_update()
        .filter(organization=organization, email__iexact=email)
        .order_by("-updated_at")[:2]
    )
    if len(email_matches) == 1:
        return email_matches[0]
    if len(email_matches) > 1:
        raise ValidationError(
            {"email": "More than one CRM lead uses this email. Review the duplicate manually."}
        )
    return None


def _apply_existing_lead_update(*, lead, name, email, attributes, policy):
    if policy == CalendarPage.AttributeUpdatePolicy.SUBMISSION_ONLY:
        return lead

    changed = []
    if policy == CalendarPage.AttributeUpdatePolicy.OVERWRITE:
        if name and name != lead.name:
            lead.name = name
            changed.append("name")
        if email and email != lead.email:
            lead.email = email
            changed.append("email")
        merged = dict(lead.attributes or {})
        for key, value in attributes.items():
            if merged.get(key) != value:
                merged[key] = value
                if "attributes" not in changed:
                    changed.append("attributes")
        lead.attributes = merged
    else:
        if email and not lead.email:
            lead.email = email
            changed.append("email")
        merged = dict(lead.attributes or {})
        for key, value in attributes.items():
            if merged.get(key) in (None, "") and value not in (None, ""):
                merged[key] = value
                if "attributes" not in changed:
                    changed.append("attributes")
        lead.attributes = merged

    if changed:
        lead.full_clean()
        lead.save(update_fields=[*changed, "updated_at"])
    return lead


@transaction.atomic
def create_submission_and_lead(
    *,
    page,
    version,
    submitted,
    normalized,
    request,
    files,
):
    organization = page.organization
    email, attributes = mapped_lead_values(
        organization=organization,
        version=version,
        normalized=normalized,
    )
    phone = normalized["mobile"]
    existing = _find_existing_lead(
        organization=organization,
        phone=phone,
        email=email,
        match_email=bool(version.snapshot.get("duplicate_match_email", True)),
    )

    if existing and version.snapshot.get("duplicate_behavior") == CalendarPage.DuplicateBehavior.BLOCK:
        submission = CalendarSubmission.objects.create(
            organization=organization,
            page=page,
            page_version=version,
            lead=existing,
            status=CalendarSubmission.Status.DUPLICATE_BLOCKED,
            submitted_data=submitted,
            normalized_data=normalized,
            attribution=attribution_from_request(request),
            consent_accepted=bool(request.POST.get("consent")),
            consent_text=str(version.snapshot.get("consent_text") or ""),
            consent_version=version.version,
            referrer=str(request.META.get("HTTP_REFERER") or "")[:200],
            user_agent=str(request.META.get("HTTP_USER_AGENT") or "")[:500],
            ip_hash=hash_ip(request.META.get("REMOTE_ADDR")),
        )
        return submission, existing, False

    created = False
    if existing:
        lead = _apply_existing_lead_update(
            lead=existing,
            name=normalized["name"],
            email=email,
            attributes=attributes,
            policy=str(
                version.snapshot.get("attribute_update_policy")
                or CalendarPage.AttributeUpdatePolicy.FILL_BLANK
            ),
        )
        status = CalendarSubmission.Status.LEAD_MATCHED
    else:
        try:
            lead = create_lead(
                organization=organization,
                pipeline=page.pipeline,
                stage=page.stage,
                name=normalized["name"],
                phone=phone,
                email=email,
                attributes=attributes,
                lead_source="shvya_calendar",
                send_welcome=False,
            )
            created = True
            status = CalendarSubmission.Status.LEAD_CREATED
        except IntegrityError:
            lead = (
                Lead.objects.select_for_update()
                .get(organization=organization, phone=phone)
            )
            lead = _apply_existing_lead_update(
                lead=lead,
                name=normalized["name"],
                email=email,
                attributes=attributes,
                policy=str(
                    version.snapshot.get("attribute_update_policy")
                    or CalendarPage.AttributeUpdatePolicy.FILL_BLANK
                ),
            )
            status = CalendarSubmission.Status.LEAD_MATCHED

    submission = CalendarSubmission.objects.create(
        organization=organization,
        page=page,
        page_version=version,
        lead=lead,
        status=status,
        submitted_data=submitted,
        normalized_data=normalized,
        attribution=attribution_from_request(request),
        consent_accepted=bool(request.POST.get("consent")),
        consent_text=str(version.snapshot.get("consent_text") or ""),
        consent_version=version.version,
        referrer=str(request.META.get("HTTP_REFERER") or "")[:200],
        user_agent=str(request.META.get("HTTP_USER_AGENT") or "")[:500],
        ip_hash=hash_ip(request.META.get("REMOTE_ADDR")),
    )

    for field in _field_schema(version):
        if str(field.get("field_type")) != "file":
            continue
        key = str(field.get("key") or "")
        upload = files.get(f"f_{key}")
        if upload is None:
            continue
        CalendarSubmissionAttachment.objects.create(
            submission=submission,
            field_key=key,
            file=upload,
            original_name=upload.name[:255],
            content_type=str(upload.content_type or "")[:120],
            size=upload.size,
        )

    transaction.on_commit(
        lambda submission_id=str(submission.id): notify_submission(submission_id)
    )
    return submission, lead, created


def notify_submission(submission_id):
    submission = (
        CalendarSubmission.objects
        .select_related("page", "page__host", "organization", "lead")
        .filter(pk=submission_id)
        .first()
    )
    if submission is None:
        return
    page = submission.page
    recipients = set()
    if page.notify_host_on_submission and page.host and page.host.email:
        recipients.add(page.host.email)
    if page.notify_user_ids:
        from apps.accounts.models import User
        recipients.update(
            User.objects.filter(
                organization=page.organization,
                id__in=page.notify_user_ids,
                is_active=True,
            ).values_list("email", flat=True)
        )
    recipients.discard("")
    if not recipients:
        return
    send_mail(
        subject=f"New SHVYA Calendar lead · {page.name}",
        message=(
            f"{submission.lead.name} submitted {page.name}.\n"
            f"Phone: {submission.lead.phone}\n"
            f"Status: {submission.get_status_display()}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=sorted(recipients),
        fail_silently=True,
    )


def _page_zone(page):
    try:
        return ZoneInfo(page.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError({"timezone": "Choose a valid IANA timezone."}) from exc


def _parse_clock(value):
    try:
        hour, minute = [int(part) for part in str(value).split(":", 1)]
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid availability time.") from exc


def _overlaps(start, end, busy_start, busy_end):
    return start < busy_end and end > busy_start


def available_slots(*, page, local_date):
    if page.status != CalendarPage.Status.PUBLISHED:
        return []
    zone = _page_zone(page)
    now = timezone.now()
    local_now = now.astimezone(zone)
    if local_date < local_now.date():
        return []
    if local_date > local_now.date() + timedelta(days=page.bookable_days):
        return []

    rule = (page.availability or {}).get(WEEKDAY_KEYS[local_date.weekday()]) or {}
    if not rule.get("enabled"):
        return []

    start_local = datetime.combine(
        local_date,
        _parse_clock(rule.get("start") or "09:00"),
        tzinfo=zone,
    )
    end_local = datetime.combine(
        local_date,
        _parse_clock(rule.get("end") or "18:00"),
        tzinfo=zone,
    )
    if start_local >= end_local:
        return []

    range_start = start_local.astimezone(UTC)
    range_end = end_local.astimezone(UTC)
    active_statuses = [
        CalendarBooking.Status.SCHEDULED,
        CalendarBooking.Status.RESCHEDULED,
    ]
    booked = list(
        CalendarBooking.objects.filter(
            page=page,
            status__in=active_statuses,
            start_at__lt=range_end,
            end_at__gt=range_start,
        ).values_list("start_at", "end_at")
    )
    blocked = list(
        CalendarBlock.objects.filter(
            page=page,
            starts_at__lt=range_end,
            ends_at__gt=range_start,
        ).values_list("starts_at", "ends_at")
    )
    try:
        google_busy = free_busy(
            page=page,
            time_min=range_start,
            time_max=range_end,
        )
    except GoogleCalendarError:
        google_busy = []

    duration = timedelta(minutes=page.slot_duration_minutes)
    before = timedelta(minutes=page.buffer_before_minutes)
    after = timedelta(minutes=page.buffer_after_minutes)
    minimum_start = now + timedelta(minutes=page.minimum_notice_minutes)
    results = []
    cursor = start_local
    while cursor + duration <= end_local and len(results) < page.max_slots_per_day:
        slot_start = cursor.astimezone(UTC)
        slot_end = (cursor + duration).astimezone(UTC)
        if slot_start >= minimum_start:
            buffered_start = slot_start - before
            buffered_end = slot_end + after
            hard_busy = [
                (a, b) for a, b in [*blocked, *google_busy]
                if _overlaps(buffered_start, buffered_end, a, b)
            ]
            overlapping_bookings = [
                (a, b) for a, b in booked
                if _overlaps(buffered_start, buffered_end, a, b)
            ]
            if not hard_busy and len(overlapping_bookings) < page.bookings_per_slot:
                results.append(
                    {
                        "start": slot_start,
                        "end": slot_end,
                        "label": cursor.strftime("%I:%M %p").lstrip("0"),
                    }
                )
        cursor += duration
    return results


def upcoming_slot_days(page, *, days=7):
    zone = _page_zone(page)
    start = timezone.now().astimezone(zone).date()
    result = []
    cursor = start
    horizon = start + timedelta(days=page.bookable_days)
    while cursor <= horizon and len(result) < days:
        slots = available_slots(page=page, local_date=cursor)
        if slots:
            result.append({"date": cursor, "slots": slots})
        cursor += timedelta(days=1)
    return result


@transaction.atomic
def _create_booking_row(*, page, submission, slot_start):
    locked_page = CalendarPage.objects.select_for_update().get(pk=page.pk)
    if locked_page.status != CalendarPage.Status.PUBLISHED:
        raise ValidationError("This booking page is not currently available.")

    duration = timedelta(minutes=locked_page.slot_duration_minutes)
    slot_end = slot_start + duration
    active_statuses = [
        CalendarBooking.Status.SCHEDULED,
        CalendarBooking.Status.RESCHEDULED,
    ]
    existing = CalendarBooking.objects.filter(
        page=locked_page,
        status__in=active_statuses,
        start_at=slot_start,
    ).count()
    if existing >= locked_page.bookings_per_slot:
        raise ValidationError("That slot was just booked. Please choose another time.")

    booking = CalendarBooking(
        organization=locked_page.organization,
        page=locked_page,
        submission=submission,
        lead=submission.lead,
        host=locked_page.host,
        start_at=slot_start,
        end_at=slot_end,
        timezone=locked_page.timezone,
        calendar_sync_status=(
            CalendarBooking.SyncStatus.PENDING
            if locked_page.host_id
            else CalendarBooking.SyncStatus.NOT_CONNECTED
        ),
    )
    if locked_page.meeting_location == CalendarPage.MeetingLocation.CUSTOM:
        booking.meeting_link = locked_page.custom_meeting_link
    booking.full_clean()
    booking.save()
    return booking


def book_slot(*, page, submission, slot_start_iso):
    if submission.page_id != page.id or not submission.lead_id:
        raise ValidationError("This lead submission cannot be booked.")

    try:
        requested = datetime.fromisoformat(
            str(slot_start_iso).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValidationError("Choose a valid booking time.") from exc
    if timezone.is_naive(requested):
        requested = requested.replace(tzinfo=_page_zone(page))
    requested = requested.astimezone(UTC)

    local_date = requested.astimezone(_page_zone(page)).date()
    valid_starts = {
        item["start"]
        for item in available_slots(page=page, local_date=local_date)
    }
    if requested not in valid_starts:
        raise ValidationError("That slot is no longer available.")

    booking = _create_booking_row(
        page=page,
        submission=submission,
        slot_start=requested,
    )
    if page.host_id:
        try:
            create_booking_event(booking)
        except GoogleCalendarError as exc:
            booking.calendar_sync_status = CalendarBooking.SyncStatus.FAILED
            booking.calendar_sync_error = str(exc)
            booking.save(
                update_fields=[
                    "calendar_sync_status",
                    "calendar_sync_error",
                    "updated_at",
                ]
            )

    schedule_booking_reminders(booking)
    return booking


TOKEN_RE = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")


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
        delivery, _created = CalendarReminderDelivery.objects.get_or_create(
            booking=booking,
            step=step,
            defaults={
                "due_at": due_at,
                "rendered_subject": render_booking_text(step.subject, booking),
                "rendered_body": render_booking_text(step.body, booking),
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


def schema_from_json(*, organization, raw):
    try:
        schema = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValidationError({"form_schema": "Form field configuration is invalid."}) from exc
    if not isinstance(schema, list):
        raise ValidationError({"form_schema": "Form fields must be a list."})

    definitions = {
        item.key: item
        for item in AttributeDefinition.objects.filter(organization=organization)
    }
    cleaned = []
    seen = set()
    for item in schema:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower()
        if not FIELD_KEY_RE.fullmatch(key) or key in seen:
            raise ValidationError({"form_schema": f"Invalid or duplicate field key: {key}"})
        seen.add(key)
        label = str(item.get("label") or "").strip()[:80]
        if not label:
            raise ValidationError({"form_schema": "Every field needs a label."})
        map_to = str(item.get("map_to") or "submission")
        field_type = str(item.get("field_type") or "text")
        options = [str(v).strip() for v in item.get("options") or [] if str(v).strip()]
        if map_to.startswith("attr:"):
            attr_key = map_to.split(":", 1)[1]
            definition = definitions.get(attr_key)
            if definition is None:
                raise ValidationError(
                    {"form_schema": f"CRM attribute {attr_key} is not available."}
                )
            mapping = {
                AttributeDefinition.FieldType.TEXT: "text",
                AttributeDefinition.FieldType.NUMERIC: "numeric",
                AttributeDefinition.FieldType.DATE: "date",
                AttributeDefinition.FieldType.DATETIME: "datetime",
                AttributeDefinition.FieldType.OPTION: "option",
            }
            field_type = mapping.get(definition.field_type, "text")
            if field_type == "option":
                options = list(definition.options or [])
        elif map_to == "email":
            field_type = "email"
        elif map_to != "submission":
            raise ValidationError({"form_schema": "Unsupported CRM field mapping."})

        cleaned.append(
            {
                "key": key,
                "label": label,
                "map_to": map_to,
                "field_type": field_type,
                "placeholder": str(item.get("placeholder") or "")[:100],
                "required": bool(item.get("required")),
                "options": options[:100],
            }
        )
    return cleaned
