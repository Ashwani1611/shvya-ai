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


from .service_common import ALLOWED_UPLOAD_EXTENSIONS, ALLOWED_UPLOAD_TYPES, FIELD_KEY_RE, MAX_UPLOAD_BYTES, logger

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
        for item in AttributeDefinition.objects.filter(is_active=True, organization=organization)
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


