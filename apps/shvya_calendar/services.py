import hashlib
import json
import logging
import re
from datetime import UTC, datetime, time, timedelta
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

from .google import (
    GoogleCalendarError,
    connection_for_page,
    create_booking_event,
    free_busy,
    update_booking_event,
)
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
ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

logger = logging.getLogger(__name__)


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


def _consent_accepted(post):
    return str(post.get("consent") or "").strip().casefold() in {
        "1",
        "true",
        "on",
        "yes",
    }


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
                filename = str(upload.name or "")
                extension = (
                    "." + filename.rsplit(".", 1)[1].casefold()
                    if "." in filename
                    else ""
                )
                if upload.size > MAX_UPLOAD_BYTES:
                    errors[key] = "File must be 10 MB or smaller."
                elif extension not in ALLOWED_UPLOAD_EXTENSIONS:
                    errors[key] = "This file extension is not allowed."
                elif upload.content_type not in ALLOWED_UPLOAD_TYPES:
                    errors[key] = "This file type is not allowed."
                submitted[key] = filename
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
        accepted = _consent_accepted(post)
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
    phone_lead = (
        Lead.objects
        .select_for_update()
        .filter(organization=organization, phone=phone)
        .first()
    )
    if not match_email or not email:
        return phone_lead

    email_matches = list(
        Lead.objects
        .select_for_update()
        .filter(organization=organization, email__iexact=email)
        .order_by("-updated_at")[:2]
    )
    if len(email_matches) > 1:
        raise ValidationError(
            {"email": "More than one CRM lead uses this email. Review the duplicate manually."}
        )

    email_lead = email_matches[0] if email_matches else None
    if phone_lead and email_lead and phone_lead.pk != email_lead.pk:
        raise ValidationError(
            {
                "form": (
                    "Mobile and email match different CRM leads. "
                    "Review the duplicate manually before merging records."
                )
            }
        )
    if email_lead and phone_lead is None and email_lead.phone != phone:
        raise ValidationError(
            {
                "form": (
                    "This email already belongs to a CRM lead with a different "
                    "mobile number. Review the existing lead before continuing."
                )
            }
        )
    return phone_lead or email_lead


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
            consent_accepted=_consent_accepted(request.POST),
            consent_text=str(version.snapshot.get("consent_text") or ""),
            consent_version=version.version,
            referrer=str(request.META.get("HTTP_REFERER") or "")[:200],
            user_agent=str(request.META.get("HTTP_USER_AGENT") or "")[:500],
            ip_hash=hash_ip(_client_ip(request)),
        )
        transaction.on_commit(
            lambda submission_id=str(submission.id): notify_submission(submission_id)
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
            # Use an inner savepoint so a concurrent unique-key race can be
            # recovered without leaving the outer submission transaction broken.
            with transaction.atomic():
                lead_name = (
                    f"{str(version.snapshot.get('lead_name_prefix') or '')}"
                    f"{normalized['name']}"
                ).strip()
                lead = create_lead(
                    organization=organization,
                    pipeline=page.pipeline,
                    stage=page.stage,
                    name=lead_name,
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
        consent_accepted=_consent_accepted(request.POST),
        consent_text=str(version.snapshot.get("consent_text") or ""),
        consent_version=version.version,
        referrer=str(request.META.get("HTTP_REFERER") or "")[:200],
        user_agent=str(request.META.get("HTTP_USER_AGENT") or "")[:500],
        ip_hash=hash_ip(_client_ip(request)),
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
    from apps.accounts.models import User
    if page.notify_user_ids:
        recipients.update(
            User.objects.filter(
                organization=page.organization,
                id__in=page.notify_user_ids,
                is_active=True,
            ).values_list("email", flat=True)
        )
    allowed_roles = {
        User.Role.ADMIN,
        User.Role.AGENT,
    }
    notify_roles = [
        role
        for role in (page.notify_roles or [])
        if role in allowed_roles
    ]
    if notify_roles:
        recipients.update(
            User.objects.filter(
                organization=page.organization,
                role__in=notify_roles,
                is_active=True,
            ).values_list("email", flat=True)
        )
    recipients.discard("")
    if recipients:
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

    if page.acknowledgement_enabled and submission.lead.email:
        variables = {
            "{{lead.name}}": submission.lead.name,
            "{{organization.name}}": page.organization.name,
            "{{page.name}}": page.name,
        }
        subject = page.acknowledgement_subject
        body = page.acknowledgement_body
        for token, value in variables.items():
            subject = subject.replace(token, str(value))
            body = body.replace(token, str(value))
        try:
            send_organization_email(
                organization=page.organization,
                to=submission.lead.email,
                subject=subject,
                text_body=body,
            )
        except EmailConfigurationError:
            # Lead intake must remain durable even when a customer's mailbox
            # is disconnected; internal staff notification above still records
            # that the submission exists.
            pass
        except Exception:
            # Provider/network failures must never turn an already committed
            # Web-to-Lead submission into an HTTP 500 for the visitor.
            logger.exception(
                "SHVYA Calendar acknowledgement delivery failed for submission %s",
                submission.id,
            )


def _page_zone(page):
    try:
        return ZoneInfo(page.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
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
    if (
        page.meeting_location == CalendarPage.MeetingLocation.GOOGLE_MEET
        and connection_for_page(page) is None
    ):
        raise GoogleCalendarError(
            "Google Calendar is not connected for this booking host yet. "
            "Your lead details are saved, but scheduling is temporarily unavailable."
        )
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
    google_busy = free_busy(
        page=page,
        time_min=range_start,
        time_max=range_end,
    )

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
    # Lock only the two rows that define the booking contract and operate with
    # scalar FK ids. This avoids lazy/stale related-object dereferences and
    # model-wide validation side effects in the public confirmation path.
    locked_page = (
        CalendarPage.objects
        .select_for_update()
        .only(
            "id",
            "organization_id",
            "host_id",
            "status",
            "slot_duration_minutes",
            "bookings_per_slot",
            "timezone",
            "meeting_location",
            "custom_meeting_link",
        )
        .get(pk=page.pk)
    )
    locked_submission = (
        CalendarSubmission.objects
        .select_for_update()
        .only("id", "page_id", "organization_id", "lead_id")
        .get(
            pk=submission.pk,
            page_id=locked_page.pk,
            organization_id=locked_page.organization_id,
        )
    )

    if locked_page.status != CalendarPage.Status.PUBLISHED:
        raise ValidationError("This booking page is not currently available.")
    if not locked_submission.lead_id:
        raise ValidationError("This lead submission cannot be booked.")

    # Verify the lead still belongs to the same organization without loading a
    # related object that may be stale in a long-lived request.
    if not Lead.objects.filter(
        pk=locked_submission.lead_id,
        organization_id=locked_page.organization_id,
    ).exists():
        raise ValidationError("This lead is no longer available for booking.")

    try:
        duration_minutes = int(locked_page.slot_duration_minutes)
        capacity_limit = int(locked_page.bookings_per_slot)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "This booking page has invalid slot settings. Please contact the team."
        ) from exc
    if duration_minutes < 5 or capacity_limit < 1:
        raise ValidationError(
            "This booking page has invalid slot settings. Please contact the team."
        )

    duration = timedelta(minutes=duration_minutes)
    slot_end = slot_start + duration
    active_statuses = [
        CalendarBooking.Status.SCHEDULED,
        CalendarBooking.Status.RESCHEDULED,
    ]

    # Browser retries/back navigation must never create two appointments for the
    # same lead-form submission.
    existing_for_submission = (
        CalendarBooking.objects
        .filter(
            submission_id=locked_submission.pk,
            status__in=active_statuses,
        )
        .order_by("-created_at")
        .first()
    )
    if existing_for_submission is not None:
        return existing_for_submission, False

    existing = CalendarBooking.objects.filter(
        page_id=locked_page.pk,
        status__in=active_statuses,
        start_at=slot_start,
    ).count()
    if existing >= capacity_limit:
        raise ValidationError("That slot was just booked. Please choose another time.")

    create_values = {
        "organization_id": locked_page.organization_id,
        "page_id": locked_page.pk,
        "submission_id": locked_submission.pk,
        "lead_id": locked_submission.lead_id,
        "host_id": locked_page.host_id,
        "start_at": slot_start,
        "end_at": slot_end,
        "timezone": locked_page.timezone,
        "calendar_sync_status": (
            CalendarBooking.SyncStatus.PENDING
            if locked_page.host_id
            else CalendarBooking.SyncStatus.NOT_CONNECTED
        ),
        # Generate before INSERT instead of relying on model.full_clean()/save
        # ordering. This keeps the public path independent from editable=False
        # unique token validation and legacy blank-token rows.
        "cancel_token": secrets.token_urlsafe(32),
        "reschedule_token": secrets.token_urlsafe(32),
    }
    if locked_page.meeting_location == CalendarPage.MeetingLocation.CUSTOM:
        create_values["meeting_link"] = locked_page.custom_meeting_link

    try:
        with transaction.atomic():
            booking = CalendarBooking.objects.create(**create_values)
    except IntegrityError as exc:
        # If another request raced us, recover the durable appointment for the
        # same submission. Otherwise convert the database error into a normal
        # slot/capacity message rather than the generic public exception toast.
        recovered = (
            CalendarBooking.objects
            .filter(
                submission_id=locked_submission.pk,
                status__in=active_statuses,
            )
            .order_by("-created_at")
            .first()
        )
        if recovered is not None:
            return recovered, False

        if CalendarBooking.objects.filter(
            page_id=locked_page.pk,
            status__in=active_statuses,
            start_at=slot_start,
        ).count() >= capacity_limit:
            raise ValidationError(
                "That slot was just booked. Please choose another time."
            ) from exc
        raise ValidationError(
            "That slot could not be reserved. Please choose another available time."
        ) from exc

    return booking, True


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

    booking, created = _create_booking_row(
        page=page,
        submission=submission,
        slot_start=requested,
    )
    if not created:
        return booking

    if page.host_id:
        try:
            create_booking_event(booking)
        except Exception as exc:
            # Calendar/Meet sync is a post-booking integration. Once the core
            # booking row exists, a provider/token/network/storage problem must
            # never make the visitor think their appointment failed.
            logger.exception(
                "SHVYA Calendar Google sync failed after booking %s was created",
                booking.id,
            )
            try:
                CalendarBooking.objects.filter(pk=booking.pk).update(
                    calendar_sync_status=CalendarBooking.SyncStatus.FAILED,
                    calendar_sync_error=str(exc)[:1000],
                    updated_at=timezone.now(),
                )
                booking.calendar_sync_status = CalendarBooking.SyncStatus.FAILED
                booking.calendar_sync_error = str(exc)[:1000]
            except Exception:
                logger.exception(
                    "Unable to persist Calendar sync failure for booking %s",
                    booking.id,
                )

    try:
        schedule_booking_reminders(booking)
    except Exception:
        # Reminder creation/delivery is also secondary to the confirmed slot.
        # Keep the appointment durable and surface operational failures in logs
        # rather than returning the visitor to slot selection.
        logger.exception(
            "SHVYA Calendar reminder scheduling failed for booking %s",
            booking.id,
        )

    return booking


def reschedule_booking(*, booking, slot_start_iso):
    page = booking.page
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

    with transaction.atomic():
        locked_page = CalendarPage.objects.select_for_update().get(pk=page.pk)
        locked = (
            CalendarBooking.objects
            .select_for_update()
            .select_related("page", "lead", "submission", "host")
            .get(pk=booking.pk)
        )
        if locked.status == CalendarBooking.Status.CANCELLED:
            raise ValidationError("Cancelled bookings cannot be rescheduled.")
        duration = timedelta(minutes=locked_page.slot_duration_minutes)
        active_statuses = [
            CalendarBooking.Status.SCHEDULED,
            CalendarBooking.Status.RESCHEDULED,
        ]
        capacity = (
            CalendarBooking.objects
            .filter(
                page=locked_page,
                status__in=active_statuses,
                start_at=requested,
            )
            .exclude(pk=locked.pk)
            .count()
        )
        if capacity >= locked_page.bookings_per_slot:
            raise ValidationError(
                "That slot was just booked. Please choose another time."
            )
        locked.previous_start_at = locked.start_at
        locked.previous_end_at = locked.end_at
        locked.start_at = requested
        locked.end_at = requested + duration
        locked.status = CalendarBooking.Status.RESCHEDULED
        locked.calendar_sync_status = (
            CalendarBooking.SyncStatus.PENDING
            if locked.host_id
            else CalendarBooking.SyncStatus.NOT_CONNECTED
        )
        locked.full_clean()
        locked.save(
            update_fields=[
                "previous_start_at",
                "previous_end_at",
                "start_at",
                "end_at",
                "status",
                "calendar_sync_status",
                "updated_at",
            ]
        )

    try:
        update_booking_event(locked)
    except GoogleCalendarError as exc:
        locked.calendar_sync_status = CalendarBooking.SyncStatus.FAILED
        locked.calendar_sync_error = str(exc)
        locked.save(
            update_fields=[
                "calendar_sync_status",
                "calendar_sync_error",
                "updated_at",
            ]
        )
    schedule_booking_reminders(locked)
    return locked


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
        delivery, _created = CalendarReminderDelivery.objects.update_or_create(
            booking=booking,
            step=step,
            defaults={
                "due_at": due_at,
                "status": CalendarReminderDelivery.Status.PENDING,
                "rendered_subject": render_booking_text(step.subject, booking),
                "rendered_body": render_booking_text(step.body, booking),
                "error": "",
                "sent_at": None,
                "completed_at": None,
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
