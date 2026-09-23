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


from .service_common import FIELD_KEY_RE

def schema_from_json(*, organization, raw):
    try:
        schema = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValidationError({"form_schema": "Form field configuration is invalid."}) from exc
    if not isinstance(schema, list):
        raise ValidationError({"form_schema": "Form fields must be a list."})

    definitions = {
        item.key: item
        for item in AttributeDefinition.objects.filter(is_active=True, organization=organization)
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


@transaction.atomic
def move_booking_pipeline(*, booking, actor, pipeline_id, stage_id):
    """A booking follows its CRM lead; preserve the booking page and host."""
    from services.crm.lead_transition import LeadTransitionError, move_lead_to_pipeline_stage

    if actor.role != User.Role.ADMIN or actor.organization_id != booking.organization_id:
        raise ValidationError("You cannot move this booking.")
    try:
        pipeline = Pipeline.objects.get(pk=pipeline_id, organization=actor.organization, is_active=True)
        stage = Stage.objects.get(pk=stage_id, pipeline=pipeline, is_active=True)
        lead = Lead.objects.select_for_update().get(pk=booking.lead_id, organization=actor.organization)
    except (Pipeline.DoesNotExist, Stage.DoesNotExist, Lead.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Choose an active pipeline and one of its stages.") from exc
    try:
        return move_lead_to_pipeline_stage(lead=lead, pipeline=pipeline, stage=stage, actor=actor)
    except LeadTransitionError as exc:
        raise ValidationError(str(exc)) from exc
