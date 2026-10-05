import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.crm.models import Lead, LeadCall, Pipeline, Stage
from apps.crm.models.lead import normalize_phone
from apps.organizations.access import crm_user_is_authorized
from services.crm.lead_service import create_lead
from services.crm_activity_service import record_call_logged

from .models import (
    CallDevice,
    CallDisposition,
    CallEvent,
    CallIntelligenceSettings,
    CallIntelligenceResult,
    CallRecord,
)

logger = logging.getLogger(__name__)


DEFAULT_DISPOSITIONS = (
    ("interested", "Interested", CallDisposition.Category.CONNECTED),
    ("not_interested", "Not Interested", CallDisposition.Category.CONNECTED),
    ("follow_up", "Follow-up", CallDisposition.Category.CONNECTED),
    ("demo_scheduled", "Demo Scheduled", CallDisposition.Category.CONNECTED),
    ("negotiation", "Negotiation", CallDisposition.Category.CONNECTED),
    ("converted", "Converted", CallDisposition.Category.CONNECTED),
    ("wrong_person", "Wrong Person", CallDisposition.Category.CONNECTED),
    ("no_answer", "No Answer", CallDisposition.Category.NOT_CONNECTED),
    ("busy", "Busy", CallDisposition.Category.NOT_CONNECTED),
    ("switched_off", "Switched Off", CallDisposition.Category.NOT_CONNECTED),
    ("rejected", "Rejected", CallDisposition.Category.NOT_CONNECTED),
    ("invalid_number", "Invalid Number", CallDisposition.Category.NOT_CONNECTED),
    ("call_back_later", "Callback Requested", CallDisposition.Category.CONNECTED),
    ("information_sent", "Information Sent", CallDisposition.Category.CONNECTED),
    ("do_not_call", "Do Not Call", CallDisposition.Category.CONNECTED),
)


TERMINAL_EVENTS = {
    CallEvent.Type.COMPLETED,
    CallEvent.Type.MISSED,
    CallEvent.Type.REJECTED,
    CallEvent.Type.BUSY,
    CallEvent.Type.NO_ANSWER,
    CallEvent.Type.FAILED,
    CallEvent.Type.RECONCILED,
}


def require_call_user(user):
    if not crm_user_is_authorized(user):
        raise ValidationError("Active organization user is required.")
    return user


def get_call_settings(organization):
    settings_obj, _ = CallIntelligenceSettings.objects.get_or_create(
        organization=organization
    )
    return settings_obj


def get_call_dispositions(organization):
    CallDisposition.objects.bulk_create(
        [
            CallDisposition(
                organization=organization,
                code=code,
                name=name,
                category=category,
                position=position,
            )
            for position, (code, name, category) in enumerate(
                DEFAULT_DISPOSITIONS, start=1
            )
        ],
        ignore_conflicts=True,
    )
    return CallDisposition.objects.filter(
        organization=organization,
        is_active=True,
    ).order_by("category", "position", "name")


def _clean_text(value, *, limit=255):
    return str(value or "").strip()[:limit]


def _positive_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_datetime(value, *, required=False):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = parse_datetime(str(value or "").strip()) if value else None
    if dt is None:
        if required:
            raise ValidationError("A valid ISO-8601 timestamp is required.")
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def normalize_call_phone(raw_phone, *, pipeline=None):
    value = _clean_text(raw_phone, limit=64)
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise ValidationError("Phone number is required.")
    if digits.startswith("00"):
        candidate = f"+{digits[2:]}"
    elif len(digits) == 11 and digits.startswith("0"):
        return normalize_call_phone(digits[1:], pipeline=pipeline)
    elif value.startswith("+"):
        candidate = f"+{digits}"
    elif len(digits) == 10:
        country = re.sub(
            r"\D",
            "",
            str(getattr(pipeline, "country_code", "") or "91"),
        ) or "91"
        candidate = f"+{country}{digits}"
    elif pipeline and getattr(pipeline, "country_code", ""):
        country = re.sub(r"\D", "", pipeline.country_code)
        candidate = f"+{digits}" if digits.startswith(country) else f"+{country}{digits}"
    else:
        candidate = f"+{digits}"
    return normalize_phone(candidate)


def resolve_user_pipeline_stage(user, settings_obj):
    """Mobile creation belongs to the authenticated employee, never a peer."""
    from apps.accounts.models import User

    pipeline = Pipeline.objects.filter(
        organization=user.organization, owner=user, is_active=True,
    ).order_by("created_at", "id").first()
    if pipeline is None:
        if user.role == User.Role.AGENT:
            raise ValidationError("Ask your administrator to assign an active pipeline before creating leads.")
        return resolve_default_pipeline_stage(settings_obj)
    stage = settings_obj.default_stage
    if stage is None or stage.pipeline_id != pipeline.id or not stage.is_active:
        stage = (pipeline.stages.filter(is_active=True, name__iexact="New Lead").first()
                 or pipeline.stages.filter(is_active=True, name__iexact="New Leads").first()
                 or pipeline.stages.filter(is_active=True).order_by("display_order", "name").first())
    if stage is None:
        raise ValidationError("The employee pipeline needs an active stage.")
    return pipeline, stage


def resolve_default_pipeline_stage(settings_obj):
    org = settings_obj.organization
    pipeline = settings_obj.default_pipeline
    if pipeline is None or pipeline.organization_id != org.id or not pipeline.is_active:
        pipeline = (
            Pipeline.objects.filter(organization=org, is_active=True, name__iexact="Leads").first()
            or Pipeline.objects.filter(organization=org, is_active=True).order_by("name").first()
        )
    if pipeline is None:
        raise ValidationError("Call Intelligence requires at least one active CRM pipeline.")

    stage = settings_obj.default_stage
    if stage is None or stage.pipeline_id != pipeline.id or not stage.is_active:
        stage = (
            Stage.objects.filter(pipeline=pipeline, is_active=True, name__iexact="New Lead").first()
            or Stage.objects.filter(pipeline=pipeline, is_active=True, name__iexact="New Leads").first()
            or Stage.objects.filter(pipeline=pipeline, is_active=True).order_by("display_order", "name").first()
        )
    if stage is None:
        raise ValidationError("Call Intelligence requires an active stage in the selected pipeline.")
    return pipeline, stage


def should_auto_create(settings_obj, direction, status):
    if not settings_obj.enabled:
        return False
    if status == CallRecord.Status.ANSWERED:
        return (
            settings_obj.auto_create_answered_incoming
            if direction == CallRecord.Direction.INCOMING
            else settings_obj.auto_create_answered_outgoing
        )
    if status == CallRecord.Status.MISSED:
        return settings_obj.auto_create_missed
    if status == CallRecord.Status.REJECTED:
        return settings_obj.auto_create_rejected
    if status == CallRecord.Status.UNKNOWN:
        return settings_obj.auto_create_unknown
    return False


def crm_status_for_call(status):
    if status == CallRecord.Status.ANSWERED:
        return "completed"
    if status == CallRecord.Status.BUSY:
        return "busy"
    return "no_response"


@transaction.atomic
def register_device(*, user, payload):
    require_call_user(user)
    device_id = _clean_text(payload.get("device_id"), limit=128)
    if not device_id:
        raise ValidationError("device_id is required.")
    defaults = {
        "user": user,
        "name": _clean_text(payload.get("name"), limit=150),
        "manufacturer": _clean_text(payload.get("manufacturer"), limit=100),
        "model": _clean_text(payload.get("model"), limit=100),
        "android_version": _clean_text(payload.get("android_version"), limit=50),
        "app_version": _clean_text(payload.get("app_version"), limit=50),
        "permissions": payload.get("permissions") if isinstance(payload.get("permissions"), dict) else {},
        "battery_optimization_ignored": bool(payload.get("battery_optimization_ignored", False)),
        "is_active": True,
        "last_seen_at": timezone.now(),
    }
    device = CallDevice.objects.select_for_update().filter(device_id=device_id).first()
    if device is not None:
        if device.organization_id != user.organization_id or device.user_id != user.id:
            raise ValidationError(
                "This Android device is already registered to another user or organization."
            )
        if not device.is_active:
            raise ValidationError("This device has been removed. Ask your administrator to reconnect it.", code="device_removed")
        for field, value in defaults.items():
            setattr(device, field, value)
    else:
        device = CallDevice(
            organization=user.organization,
            device_id=device_id,
            **defaults,
        )
    device.full_clean()
    device.save()
    return device


def ingest_call_event(*, user, payload):
    """Return the committed event when concurrent requests use the same UUID.

    The inner atomic block rolls back all call and CRM side effects if the
    unique event insert loses a race. Query the winner only after rollback.
    """
    try:
        return _ingest_call_event_atomic(user=user, payload=payload)
    except IntegrityError:
        try:
            event_uuid = uuid.UUID(str(payload.get("event_uuid") or ""))
        except (AttributeError, TypeError, ValueError):
            raise
        existing_event = (
            CallEvent.objects.select_related("call", "call__lead", "call__crm_call")
            .filter(event_uuid=event_uuid)
            .first()
        )
        if existing_event is None:
            raise
        if existing_event.organization_id != user.organization_id:
            raise ValidationError("Event identity belongs to another organization.")
        return {
            "call": existing_event.call,
            "event": existing_event,
            "event_created": False,
            "lead_created": False,
        }


@transaction.atomic
def _ingest_call_event_atomic(*, user, payload):
    require_call_user(user)
    if not isinstance(payload, dict):
        raise ValidationError("Expected a JSON object.")
    try:
        event_uuid = uuid.UUID(str(payload.get("event_uuid") or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("event_uuid must be a valid UUID.") from exc

    existing_event = (
        CallEvent.objects.select_related("call", "call__lead", "call__crm_call")
        .filter(event_uuid=event_uuid)
        .first()
    )
    if existing_event:
        if existing_event.organization_id != user.organization_id:
            raise ValidationError("Event identity belongs to another organization.")
        return {"call": existing_event.call, "event": existing_event, "event_created": False, "lead_created": False}

    source = _clean_text(payload.get("source"), limit=24) or CallRecord.Source.ANDROID_SIM
    if source not in CallRecord.Source.values:
        raise ValidationError("Unsupported call source.")

    device_id = _clean_text(payload.get("device_id"), limit=128)
    device = None
    if source == CallRecord.Source.ANDROID_SIM:
        if not device_id:
            raise ValidationError("device_id is required for Android SIM calls.")
        device = (
            CallDevice.objects.select_for_update()
            .filter(organization=user.organization, device_id=device_id, is_active=True)
            .first()
        )
        if device is None:
            device = register_device(
                user=user,
                payload={
                    "device_id": device_id,
                    "name": payload.get("device_name", ""),
                    "app_version": payload.get("app_version", ""),
                },
            )
        elif device.user_id != user.id:
            raise ValidationError("This Android device is already registered to another user.")
    elif device_id:
        device = (
            CallDevice.objects.select_for_update()
            .filter(
                organization=user.organization,
                user=user,
                device_id=device_id,
                is_active=True,
            )
            .first()
        )

    settings_obj = get_call_settings(user.organization)
    normalization_pipeline = settings_obj.default_pipeline
    if source == CallRecord.Source.ANDROID_SIM:
        normalization_pipeline = (
            Pipeline.objects.filter(organization=user.organization, owner=user, is_active=True)
            .order_by("created_at", "id").first() or normalization_pipeline
        )
    phone = normalize_call_phone(
        payload.get("phone_number") or payload.get("raw_phone_number"),
        pipeline=normalization_pipeline,
    )
    source_call_id = _clean_text(payload.get("source_call_id"))
    if not source_call_id:
        raise ValidationError("source_call_id is required.")

    direction = _clean_text(payload.get("direction"), limit=16)
    if direction not in CallRecord.Direction.values:
        raise ValidationError("direction must be incoming or outgoing.")
    call_status = _clean_text(payload.get("status"), limit=20) or CallRecord.Status.UNKNOWN
    if call_status not in CallRecord.Status.values:
        raise ValidationError("Unsupported call status.")
    event_type = _clean_text(payload.get("event_type"), limit=20)
    if event_type not in CallEvent.Type.values:
        raise ValidationError("Unsupported event_type.")

    occurred_at = _parse_datetime(payload.get("occurred_at"), required=True)
    started_at = _parse_datetime(payload.get("started_at"))
    ringing_at = _parse_datetime(payload.get("ringing_at"))
    answered_at = _parse_datetime(payload.get("answered_at"))
    ended_at = _parse_datetime(payload.get("ended_at")) or (
        occurred_at if event_type in TERMINAL_EVENTS else None
    )

    record, record_created = CallRecord.objects.select_for_update().get_or_create(
        organization=user.organization,
        source=source,
        source_call_id=source_call_id,
        defaults={
            "device": device,
            "user": user,
            "provider": _clean_text(payload.get("provider"), limit=80),
            "provider_call_id": _clean_text(payload.get("provider_call_id")),
            "sim_account_id": _clean_text(payload.get("sim_account_id")),
            "phone_number": phone,
            "raw_phone_number": _clean_text(payload.get("raw_phone_number") or payload.get("phone_number"), limit=64),
            "contact_name": _clean_text(payload.get("contact_name")),
            "direction": direction,
            "status": call_status,
            "sub_status": _clean_text(payload.get("sub_status"), limit=40),
            "call_name": _clean_text(payload.get("call_name"), limit=150) or "Phone Call",
            "started_at": started_at,
            "ringing_at": ringing_at,
            "answered_at": answered_at,
            "ended_at": ended_at,
            "ring_duration_seconds": _positive_int(payload.get("ring_duration_seconds")),
            "talk_duration_seconds": _positive_int(payload.get("talk_duration_seconds")),
            "total_duration_seconds": _positive_int(payload.get("total_duration_seconds")),
            "recording_url": _clean_text(payload.get("recording_url"), limit=1000),
            "recording_status": _clean_text(payload.get("recording_status"), limit=32),
            "transcript_status": _clean_text(payload.get("transcript_status"), limit=32),
            "transcript": _clean_text(payload.get("transcript"), limit=100000),
            "transcript_speakers": payload.get("transcript_speakers")
            if isinstance(payload.get("transcript_speakers"), list) else [],
            "notes": _clean_text(payload.get("notes"), limit=20000),
        },
    )
    if not record_created:
        if (
            source == CallRecord.Source.ANDROID_SIM
            and record.device_id
            and device
            and record.device_id != device.id
        ):
            raise ValidationError("source_call_id already belongs to another Android device.")
        record.device = record.device or device
        record.user = user
        record.provider = _clean_text(payload.get("provider"), limit=80) or record.provider
        record.provider_call_id = _clean_text(payload.get("provider_call_id")) or record.provider_call_id
        record.sim_account_id = _clean_text(payload.get("sim_account_id")) or record.sim_account_id
        record.phone_number = phone
        record.raw_phone_number = _clean_text(payload.get("raw_phone_number") or record.raw_phone_number, limit=64)
        record.contact_name = _clean_text(payload.get("contact_name")) or record.contact_name
        record.direction = direction
        # Mobile outboxes can deliver ringing/started events after completion.
        # Keep the final outcome and CRM call authoritative in that case.
        if record.ended_at is None or event_type in TERMINAL_EVENTS:
            record.status = call_status
        record.sub_status = _clean_text(payload.get("sub_status"), limit=40) or record.sub_status
        record.started_at = started_at or record.started_at
        record.ringing_at = ringing_at or record.ringing_at
        record.answered_at = answered_at or record.answered_at
        if event_type in TERMINAL_EVENTS or record.ended_at is None:
            record.ended_at = ended_at or record.ended_at
        record.ring_duration_seconds = max(record.ring_duration_seconds, _positive_int(payload.get("ring_duration_seconds")))
        record.talk_duration_seconds = max(record.talk_duration_seconds, _positive_int(payload.get("talk_duration_seconds")))
        record.total_duration_seconds = max(record.total_duration_seconds, _positive_int(payload.get("total_duration_seconds")))
        record.recording_url = _clean_text(payload.get("recording_url"), limit=1000) or record.recording_url
        record.recording_status = _clean_text(payload.get("recording_status"), limit=32) or record.recording_status
        record.transcript_status = _clean_text(payload.get("transcript_status"), limit=32) or record.transcript_status
        incoming_transcript = _clean_text(payload.get("transcript"), limit=100000)
        if incoming_transcript:
            record.transcript = incoming_transcript
        if isinstance(payload.get("transcript_speakers"), list):
            record.transcript_speakers = payload["transcript_speakers"][:1000]
        incoming_notes = _clean_text(payload.get("notes"), limit=20000)
        if incoming_notes:
            record.notes = incoming_notes

    lead = record.lead or (
        Lead.objects.select_related("pipeline", "stage")
        .filter(organization=user.organization, phone=phone)
        .first()
    )
    lead_created = False
    terminal = event_type in TERMINAL_EVENTS
    if lead is None and terminal and should_auto_create(settings_obj, direction, call_status):
        pipeline, stage = (resolve_user_pipeline_stage(user, settings_obj)
                           if source == CallRecord.Source.ANDROID_SIM
                           else resolve_default_pipeline_stage(settings_obj))
        lead = create_lead(
            organization=user.organization,
            pipeline=pipeline,
            stage=stage,
            name=_clean_text(payload.get("contact_name")) or f"Phone Lead {phone[-4:]}",
            phone=phone,
            lead_source="phone_call",
            send_welcome=False,
        )
        lead_created = True

    if lead is not None:
        attach_call_to_lead(record, lead, terminal=terminal, occurred_at=occurred_at)

    record.sync_status = "synced"
    record.last_sync_attempt_at = timezone.now()
    record.sync_error_message = ""
    record.save()

    if terminal or record.ended_at:
        request_call_analysis(record.id)

    event = CallEvent.objects.create(
        event_uuid=event_uuid,
        organization=user.organization,
        call=record,
        device=device,
        user=user,
        event_type=event_type,
        occurred_at=occurred_at,
        payload={
            key: ("[stored on call]" if key in {"transcript", "transcript_speakers"} else value)
            for key, value in payload.items()
        },
    )
    if device is not None:
        device.last_seen_at = timezone.now()
        device.app_version = _clean_text(
            payload.get("app_version") or device.app_version,
            limit=50,
        )
        device.save(update_fields=["last_seen_at", "app_version", "updated_at"])
    return {"call": record, "event": event, "event_created": True, "lead_created": lead_created}


def capture_manual_crm_call(call):
    """Mirror a CRM manual call once, retaining its original lead and employee."""
    record = CallRecord.objects.get_or_create(
        crm_call=call,
        defaults={
            "organization": call.lead.organization,
            "lead": call.lead,
            "user": call.user,
            "source": CallRecord.Source.MANUAL,
            "source_call_id": f"crm-{call.id}",
            "phone_number": call.lead.phone,
            "contact_name": call.lead.name,
            "direction": CallRecord.Direction.OUTGOING,
            "status": {"completed": "answered", "busy": "busy"}.get(call.status, "no_answer"),
            "call_name": call.call_name,
            "talk_duration_seconds": call.duration_seconds or 0,
            "total_duration_seconds": call.duration_seconds or 0,
            "started_at": call.called_at,
            "ended_at": call.called_at + timedelta(seconds=call.duration_seconds or 0),
            "notes": call.notes,
        },
    )[0]
    request_call_analysis(record.id)
    return record


def create_crm_lead_from_call(*, user, call_id, name, pipeline_id, stage_id, email=""):
    """Create in an authorized CRM destination and attach the original call once."""
    from apps.crm.views.api import get_user_pipelines

    require_call_user(user)
    try:
        pipeline_id = uuid.UUID(str(pipeline_id))
        stage_id = uuid.UUID(str(stage_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Choose an available pipeline and stage.") from exc
    pipeline = get_user_pipelines(user).filter(pk=pipeline_id).first()
    if pipeline is None:
        raise ValidationError("Pipeline is not available to you.")
    stage = Stage.objects.filter(pk=stage_id, pipeline=pipeline, is_active=True).first()
    if stage is None:
        raise ValidationError("Choose an active stage from the selected pipeline.")
    name = str(name or "").strip()
    if not name:
        raise ValidationError("Lead name is required.")

    with transaction.atomic():
        calls = CallRecord.objects.select_for_update().filter(
            organization=user.organization, pk=call_id,
        )
        if user.role == "agent":
            calls = calls.filter(user=user)
        call = calls.first()
        if call is None:
            raise ValidationError("Call is not available.", code="call_not_found")
        created = False
        lead = call.lead
        if lead is None:
            phone = normalize_call_phone(call.phone_number, pipeline=pipeline)
            lead = Lead.objects.filter(organization=user.organization, phone=phone).first()
            if lead is None:
                try:
                    with transaction.atomic():
                        lead = create_lead(
                            organization=user.organization, pipeline=pipeline, stage=stage,
                            name=name, phone=phone, email=str(email or "").strip(),
                            lead_source="phone_call", send_welcome=False,
                        )
                        created = True
                except (ValidationError, IntegrityError):
                    # A CRM save or another call may win the organization/phone race.
                    lead = Lead.objects.filter(organization=user.organization, phone=phone).first()
                    if lead is None:
                        raise
            call.phone_number = phone
        attach_call_to_lead(call, lead, terminal=call.ended_at is not None)
        call.save(update_fields=["lead", "crm_call", "phone_number", "updated_at"])
        request_call_analysis(call.id)
        return call, created


def attach_call_to_lead(record, lead, *, terminal, occurred_at=None):
    """The caller holds the CallRecord lock; never move historical calls to a peer."""
    if record.organization_id != lead.organization_id:
        raise ValidationError("Call and lead must belong to the same organization.")
    if record.lead_id and record.lead_id != lead.id:
        raise ValidationError("Call already belongs to another CRM lead.")
    record.lead = lead
    if not terminal:
        return
    if record.crm_call_id is None:
        record.crm_call = LeadCall.objects.create(
            lead=lead,
            user=record.user,
            status=crm_status_for_call(record.status),
            call_name=record.call_name or "Phone Call",
            duration_seconds=record.talk_duration_seconds,
            notes=record.notes,
            called_at=record.started_at or record.ended_at or occurred_at or record.created_at,
        )
        record_call_logged(lead=lead, actor=record.user, call=record.crm_call)
    else:
        crm_call = record.crm_call
        if crm_call.lead_id != lead.id:
            raise ValidationError("CRM call does not belong to the matched lead.")
        crm_call.status = crm_status_for_call(record.status)
        crm_call.duration_seconds = record.talk_duration_seconds
        crm_call.notes = record.notes
        crm_call.save(update_fields=["status", "duration_seconds", "notes"])


def reconcile_lead_calls(lead_id):
    """Link earlier calls after CRM creation/phone editing, including dialer prefixes."""
    lead = Lead.objects.select_related("pipeline", "stage").filter(pk=lead_id).first()
    if lead is None or not lead.phone:
        return
    digits = re.sub(r"\D", "", lead.phone)
    phones = {lead.phone, digits, f"+00{digits}", f"00{digits}"}
    country = re.sub(r"\D", "", lead.pipeline.country_code or "91")
    if country and digits.startswith(country) and len(digits[len(country):]) == 10:
        local = digits[len(country):]
        phones.update({local, f"+{local}", f"0{local}", f"+0{local}"})
    ids = list(CallRecord.objects.filter(
        organization_id=lead.organization_id, phone_number__in=phones, lead__isnull=True,
    ).values_list("id", flat=True))
    for call_id in ids:
        reconcile_call(call_id, lead=lead)


@transaction.atomic
def reconcile_call(call_id, *, lead=None):
    record = CallRecord.objects.select_for_update().filter(pk=call_id).first()
    if record is None:
        return
    lead = lead or record.lead or Lead.objects.filter(
        organization_id=record.organization_id, phone=record.phone_number,
    ).first()
    if lead is None:
        return
    attach_call_to_lead(record, lead, terminal=record.ended_at is not None)
    record.save(update_fields=["lead", "crm_call", "updated_at"])
    if record.ended_at:
        request_call_analysis(record.id)


def reconcile_call_tracking(user, *, limit=100):
    """Repair legacy matching before filtering analytics; skip genuine unknown callers."""
    matches = Lead.objects.filter(
        organization_id=OuterRef("organization_id"), phone=OuterRef("phone_number"),
    )
    qs = CallRecord.objects.filter(organization=user.organization).annotate(
        has_lead=Exists(matches),
    ).filter(
        Q(lead__isnull=True, has_lead=True)
        | Q(lead__isnull=False, crm_call__isnull=True, ended_at__isnull=False),
    )
    if user.role == "agent":
        qs = qs.filter(user=user)
    for call_id in list(qs.values_list("id", flat=True)[:limit]):
        reconcile_call(call_id)


def call_analysis_hash(call):
    """Tie results to the evidence version, so late workers cannot overwrite new notes."""
    evidence = {
        "notes": call.notes, "transcript": call.transcript,
        "speaker_segments": call.transcript_speakers, "disposition": call.disposition,
        "status": call.status, "direction": call.direction,
        "talk_duration_seconds": call.talk_duration_seconds,
        "ring_duration_seconds": call.ring_duration_seconds,
        "lead_id": str(call.lead_id or ""),
    }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def publish_call_analysis(call_id, input_hash):
    from .tasks import analyze_call_intelligence

    try:
        analyze_call_intelligence.delay(str(call_id), input_hash)
    except Exception:
        # Keep the durable queued row for Beat to recover; notes are already saved.
        logger.warning("Call Intelligence queue publication failed for %s", call_id)
        CallRecord.objects.filter(pk=call_id, analysis_input_hash=input_hash).update(
            analysis_error="Notes saved. Analysis is waiting for the worker to reconnect.",
        )


@transaction.atomic
def request_call_analysis(call_id, *, retry_failed=False):
    call = CallRecord.objects.select_for_update().get(pk=call_id)
    if not call.notes.strip() and not call.transcript.strip():
        CallIntelligenceResult.objects.filter(call=call).delete()
        call.analysis_status = CallRecord.AnalysisStatus.NOT_REQUESTED
        call.analysis_input_hash = ""
        call.analysis_error = ""
        call.analysis_attempts = 0
    else:
        input_hash = call_analysis_hash(call)
        if call.analysis_input_hash == input_hash and (
            call.analysis_status != CallRecord.AnalysisStatus.FAILED or not retry_failed
        ):
            return call
        call.analysis_status = CallRecord.AnalysisStatus.QUEUED
        call.analysis_input_hash = input_hash
        call.analysis_error = ""
        call.analysis_attempts = 0
        CallIntelligenceResult.objects.filter(call=call).delete()
        transaction.on_commit(
            lambda: publish_call_analysis(call.id, input_hash)
        )
    call.analysis_updated_at = timezone.now()
    call.save(update_fields=[
        "analysis_status", "analysis_input_hash", "analysis_error",
        "analysis_attempts", "analysis_updated_at",
    ])
    return call
