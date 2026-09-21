import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.models import Lead, LeadCall, LeadReminder, Pipeline, Stage
from services.crm.lead_service import create_lead
from services.crm_activity_service import record_call_logged

from .models import (
    CallDevice,
    CallEvent,
    CallIntelligence,
    CallIntelligenceSettings,
    CallRecord,
)


TERMINAL_STATUSES = {
    CallRecord.Status.COMPLETED,
    CallRecord.Status.MISSED,
    CallRecord.Status.REJECTED,
    CallRecord.Status.BUSY,
    CallRecord.Status.NO_ANSWER,
    CallRecord.Status.FAILED,
}


def visible_pipelines(user):
    if not user or not user.organization_id:
        return Pipeline.objects.none()
    if user.role == User.Role.ADMIN:
        return Pipeline.objects.filter(
            organization_id=user.organization_id,
            is_active=True,
        )
    if user.role == User.Role.AGENT:
        return Pipeline.objects.filter(
            organization_id=user.organization_id,
            owner=user,
            is_active=True,
        )
    return Pipeline.objects.none()


def visible_calls(user):
    qs = (
        CallRecord.objects
        .filter(organization_id=user.organization_id)
        .select_related(
            "lead",
            "pipeline",
            "stage",
            "user",
            "device",
            "intelligence",
        )
    )
    if user.role == User.Role.AGENT:
        return qs.filter(user=user)
    return qs


def get_call_settings(organization):
    settings_obj, _ = CallIntelligenceSettings.objects.get_or_create(
        organization=organization,
    )
    return settings_obj


def normalize_call_phone(raw_phone, *, settings_obj):
    value = str(raw_phone or "").strip()
    if not value:
        raise ValidationError({"phone_number": "Phone number is required."})

    has_plus = value.startswith("+")
    digits = re.sub(r"\D", "", value)
    if len(digits) < 8:
        raise ValidationError({"phone_number": "Phone number is too short."})

    if has_plus:
        return f"+{digits}"

    country_code = re.sub(
        r"\D",
        "",
        str(settings_obj.default_country_code or "+91"),
    )
    if not country_code:
        raise ValidationError(
            {"phone_number": "A default country code is required for local numbers."}
        )

    # Internal Android deployment currently targets Indian employee devices.
    # A normal 10-digit local number is promoted to the organization country code.
    if len(digits) == 10:
        return f"+{country_code}{digits}"

    # If Android already returned country code digits without '+', preserve them.
    if digits.startswith(country_code):
        return f"+{digits}"

    return f"+{country_code}{digits}"


def _resolve_default_destination(*, user, settings_obj):
    pipelines = visible_pipelines(user)
    pipeline = None
    if settings_obj.default_pipeline_id:
        pipeline = pipelines.filter(pk=settings_obj.default_pipeline_id).first()
    if pipeline is None:
        pipeline = pipelines.order_by("name").first()
    if pipeline is None:
        return None, None

    stage = None
    if (
        settings_obj.default_stage_id
        and settings_obj.default_stage.pipeline_id == pipeline.id
    ):
        stage = Stage.objects.filter(
            pk=settings_obj.default_stage_id,
            pipeline=pipeline,
            is_active=True,
        ).first()

    if stage is None:
        stage = (
            Stage.objects.filter(
                pipeline=pipeline,
                is_active=True,
                name__iexact="New Lead",
            ).first()
            or Stage.objects.filter(
                pipeline=pipeline,
                is_active=True,
                name__iexact="New Leads",
            ).first()
            or Stage.objects.filter(
                pipeline=pipeline,
                is_active=True,
            ).order_by("display_order", "name").first()
        )
    return pipeline, stage


def _accessible_lead(*, user, phone_number):
    allowed = visible_pipelines(user)
    return (
        Lead.objects
        .filter(
            organization_id=user.organization_id,
            phone=phone_number,
            pipeline__in=allowed,
        )
        .select_related("pipeline", "stage")
        .first()
    )


def _organization_has_phone(*, user, phone_number):
    return Lead.objects.filter(
        organization_id=user.organization_id,
        phone=phone_number,
    ).exists()


def _should_auto_create(*, call, settings_obj):
    if not settings_obj.is_enabled:
        return False
    if call.direction == CallRecord.Direction.OUTBOUND:
        return settings_obj.auto_create_outbound and call.status == CallRecord.Status.COMPLETED
    if call.status == CallRecord.Status.MISSED:
        return settings_obj.auto_create_missed
    return (
        settings_obj.auto_create_answered
        and call.status == CallRecord.Status.COMPLETED
    )


def _lead_name(call):
    contact = str(call.contact_name or "").strip()
    if contact:
        return contact[:150]
    suffix = re.sub(r"\D", "", call.phone_number)[-4:]
    return f"Phone Lead {suffix}"


def _attach_or_create_lead(*, call, user, settings_obj):
    lead = _accessible_lead(user=user, phone_number=call.phone_number)
    if lead is not None:
        call.lead = lead
        call.pipeline = lead.pipeline
        call.stage = lead.stage
        call.lead_match_status = CallRecord.LeadMatchStatus.MATCHED
        return

    # Do not leak or duplicate a lead that exists in another agent's pipeline.
    if _organization_has_phone(user=user, phone_number=call.phone_number):
        call.lead_match_status = CallRecord.LeadMatchStatus.RESTRICTED
        return

    if call.status not in TERMINAL_STATUSES or not _should_auto_create(
        call=call,
        settings_obj=settings_obj,
    ):
        call.lead_match_status = CallRecord.LeadMatchStatus.UNMATCHED
        return

    pipeline, stage = _resolve_default_destination(
        user=user,
        settings_obj=settings_obj,
    )
    if pipeline is None or stage is None:
        call.lead_match_status = CallRecord.LeadMatchStatus.UNMATCHED
        return

    lead = create_lead(
        organization=user.organization,
        pipeline=pipeline,
        stage=stage,
        name=_lead_name(call),
        phone=call.phone_number,
        lead_source="call_intelligence",
        send_welcome=False,
        notes="Created automatically from SHVYA Call Intelligence.",
        attributes={
            "CALL SOURCE": "Android SIM",
            "CALL DIRECTION": call.direction,
        },
    )
    call.lead = lead
    call.pipeline = lead.pipeline
    call.stage = lead.stage
    call.lead_match_status = CallRecord.LeadMatchStatus.AUTO_CREATED


def _crm_status(call):
    if call.status == CallRecord.Status.COMPLETED:
        return "completed"
    if call.status == CallRecord.Status.BUSY:
        return "busy"
    if call.status in {
        CallRecord.Status.MISSED,
        CallRecord.Status.REJECTED,
        CallRecord.Status.NO_ANSWER,
        CallRecord.Status.FAILED,
    }:
        return "no_response"
    return "scheduled"


def _sync_crm_call(call):
    if call.lead_id is None or call.status not in TERMINAL_STATUSES:
        return

    call_name = (
        "Incoming Phone Call"
        if call.direction == CallRecord.Direction.INBOUND
        else "Outgoing Phone Call"
        if call.direction == CallRecord.Direction.OUTBOUND
        else "Phone Call"
    )
    created = False
    crm_call = call.crm_call
    if crm_call is None:
        crm_call = LeadCall.objects.create(
            lead=call.lead,
            user=call.user,
            status=_crm_status(call),
            call_name=call_name,
            duration_seconds=call.duration_seconds,
            notes=call.notes,
            called_at=call.called_at,
        )
        call.crm_call = crm_call
        created = True
    else:
        crm_call.lead = call.lead
        crm_call.user = call.user
        crm_call.status = _crm_status(call)
        crm_call.call_name = call_name
        crm_call.duration_seconds = call.duration_seconds
        crm_call.notes = call.notes
        crm_call.called_at = call.called_at
        crm_call.save(
            update_fields=[
                "lead",
                "user",
                "status",
                "call_name",
                "duration_seconds",
                "notes",
                "called_at",
            ]
        )

    if created:
        record_call_logged(
            lead=call.lead,
            actor=call.user,
            call=crm_call,
        )


EVENT_STATUS = {
    CallEvent.Type.RINGING: CallRecord.Status.RINGING,
    CallEvent.Type.OFFHOOK: CallRecord.Status.ANSWERED,
    CallEvent.Type.ANSWERED: CallRecord.Status.ANSWERED,
    CallEvent.Type.COMPLETED: CallRecord.Status.COMPLETED,
    CallEvent.Type.MISSED: CallRecord.Status.MISSED,
    CallEvent.Type.REJECTED: CallRecord.Status.REJECTED,
}


@transaction.atomic
def ingest_call_event(*, user, validated_data):
    if not user.organization_id:
        raise ValidationError("An organization user is required.")

    device = (
        CallDevice.objects.select_for_update()
        .filter(
            organization_id=user.organization_id,
            user=user,
            device_uuid=validated_data["device_id"],
            is_active=True,
        )
        .first()
    )
    if device is None:
        raise ValidationError({"device_id": "Register this Android device first."})

    event_uuid = validated_data["event_uuid"]
    existing_event = (
        CallEvent.objects
        .select_related("call")
        .filter(event_uuid=event_uuid)
        .first()
    )
    if existing_event is not None:
        if existing_event.call.organization_id != user.organization_id:
            raise ValidationError({"event_uuid": "Event identifier is not valid."})
        return existing_event.call, False

    settings_obj = get_call_settings(user.organization)
    phone = normalize_call_phone(
        validated_data.get("phone_number")
        or validated_data.get("raw_phone_number"),
        settings_obj=settings_obj,
    )
    source_call_id = str(validated_data.get("source_call_id") or "").strip()

    defaults = {
        "user": user,
        "device": device,
        "direction": validated_data.get("direction") or CallRecord.Direction.UNKNOWN,
        "status": validated_data.get("status") or CallRecord.Status.UNKNOWN,
        "phone_number": phone,
        "raw_phone_number": validated_data.get("raw_phone_number") or "",
        "contact_name": validated_data.get("contact_name") or "",
        "sim_slot": validated_data.get("sim_slot") or "",
        "started_at": validated_data.get("started_at"),
        "ringing_at": validated_data.get("ringing_at"),
        "answered_at": validated_data.get("answered_at"),
        "ended_at": validated_data.get("ended_at"),
        "called_at": (
            validated_data.get("started_at")
            or validated_data.get("occurred_at")
            or timezone.now()
        ),
        "ring_duration_seconds": validated_data.get("ring_duration_seconds") or 0,
        "duration_seconds": validated_data.get("duration_seconds") or 0,
        "metadata": validated_data.get("metadata") or {},
    }

    if source_call_id:
        call, created = CallRecord.objects.select_for_update().get_or_create(
            organization=user.organization,
            source=CallRecord.Source.ANDROID_SIM,
            source_call_id=source_call_id,
            defaults=defaults,
        )
    else:
        call = CallRecord.objects.create(
            organization=user.organization,
            source=CallRecord.Source.ANDROID_SIM,
            source_call_id="",
            **defaults,
        )
        created = True

    # Newer Android events progressively enrich the same record. Never blank a
    # known field merely because one state broadcast does not contain it.
    call.user = user
    call.device = device
    call.phone_number = phone
    for field in (
        "raw_phone_number",
        "contact_name",
        "sim_slot",
        "started_at",
        "ringing_at",
        "answered_at",
        "ended_at",
    ):
        value = validated_data.get(field)
        if value not in (None, ""):
            setattr(call, field, value)

    for field in ("ring_duration_seconds", "duration_seconds"):
        value = validated_data.get(field)
        if value is not None:
            setattr(call, field, max(getattr(call, field), int(value)))

    if validated_data.get("direction"):
        call.direction = validated_data["direction"]
    incoming_status = validated_data.get("status")
    event_status = EVENT_STATUS.get(validated_data["event_type"])
    if incoming_status:
        call.status = incoming_status
    elif event_status:
        call.status = event_status

    if validated_data.get("metadata"):
        call.metadata = {
            **(call.metadata or {}),
            **validated_data["metadata"],
        }

    _attach_or_create_lead(
        call=call,
        user=user,
        settings_obj=settings_obj,
    )
    call.save()

    CallEvent.objects.create(
        call=call,
        device=device,
        event_uuid=event_uuid,
        event_type=validated_data["event_type"],
        occurred_at=validated_data["occurred_at"],
        payload=validated_data.get("payload") or {},
    )

    _sync_crm_call(call)
    if call.crm_call_id:
        call.save(update_fields=["crm_call", "updated_at"])

    CallIntelligence.objects.get_or_create(call=call)
    return call, created


@transaction.atomic
def update_call_notes(*, call, notes):
    call.notes = str(notes or "").strip()
    call.save(update_fields=["notes", "updated_at"])
    if call.crm_call_id:
        call.crm_call.notes = call.notes
        call.crm_call.save(update_fields=["notes"])
    return call


@transaction.atomic
def set_call_follow_up(*, call, assigned_to, due_at, title="", description=""):
    if call.lead_id is None:
        raise ValidationError("A CRM lead is required before a follow-up can be created.")

    reminder = LeadReminder.objects.create(
        lead=call.lead,
        assigned_to=assigned_to,
        title=(title or "Call follow-up")[:200],
        description=description or f"Follow up after {call.get_direction_display().lower()} call.",
        due_at=due_at,
        status="pending",
    )
    call.follow_up_required = True
    call.follow_up_at = due_at
    call.save(update_fields=["follow_up_required", "follow_up_at", "updated_at"])
    return reminder
