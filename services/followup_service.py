"""Business logic for SHVYA Auto Follow-ups.

The service owns sequence authoring, lead assignment/progress, scheduling,
serialized execution, WhatsApp template delivery, future email delivery, and
CRM reminder creation. Views only validate request shape and call this layer.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models import LeadReminder
from apps.followups.models import (
    AutoFollowupSettings,
    FollowupExecution,
    FollowupSenderState,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from services.channels.template_service import render_template_body


WHATSAPP_MIN_SEND_GAP_SECONDS = 60
DISPATCH_LOCK_KEY = "shvya:auto-followups:dispatcher"
DISPATCH_LOCK_SECONDS = 55


class FollowupError(Exception):
    pass


def get_auto_followup_settings(organization):
    result, _ = AutoFollowupSettings.objects.get_or_create(organization=organization)
    return result


def update_auto_followup_settings(
    organization,
    *,
    enabled,
    business_hours_start,
    business_hours_end,
    conversation_delay_value,
    conversation_delay_unit,
):
    if business_hours_start is None or business_hours_end is None:
        raise FollowupError("Business Hours start and end are required.")
    if business_hours_end <= business_hours_start:
        raise FollowupError("Business Hours end time must be later than the start time.")
    if conversation_delay_value < 0 or conversation_delay_value > 30:
        raise FollowupError("Conversation delay must be between 0 and 30.")
    if conversation_delay_unit not in AutoFollowupSettings.DelayUnit.values:
        raise FollowupError("Invalid conversation delay unit.")

    config = get_auto_followup_settings(organization)
    config.enabled = bool(enabled)
    config.business_hours_start = business_hours_start
    config.business_hours_end = business_hours_end
    config.conversation_delay_value = conversation_delay_value
    config.conversation_delay_unit = conversation_delay_unit
    config.save()
    return config


def create_sequence(*, organization, created_by, name, description, whatsapp_account):
    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        raise FollowupError("Sequence name is required.")
    if len(name) > 255:
        raise FollowupError("Sequence name must be 255 characters or fewer.")
    if len(description) > 300:
        raise FollowupError("Description must be 300 characters or fewer.")
    if whatsapp_account.organization_id != organization.id:
        raise FollowupError("Selected WhatsApp API number does not belong to this organization.")
    if whatsapp_account.status != WhatsAppAccount.Status.CONNECTED or not whatsapp_account.is_active:
        raise FollowupError("Select an active connected WhatsApp API number.")
    if FollowupSequence.objects.filter(organization=organization, name__iexact=name).exists():
        raise FollowupError("A sequence with this name already exists.")
    return FollowupSequence.objects.create(
        organization=organization,
        created_by=created_by,
        name=name,
        description=description,
        whatsapp_account=whatsapp_account,
    )


def update_sequence(*, sequence, name, description):
    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        raise FollowupError("Sequence name is required.")
    if len(name) > 255 or len(description) > 300:
        raise FollowupError("Sequence name or description is too long.")
    duplicate = FollowupSequence.objects.filter(
        organization=sequence.organization,
        name__iexact=name,
    ).exclude(id=sequence.id).exists()
    if duplicate:
        raise FollowupError("A sequence with this name already exists.")
    sequence.name = name
    sequence.description = description
    sequence.save(update_fields=["name", "description", "updated_at"])
    return sequence


def duplicate_sequence(*, sequence, created_by):
    base = f"{sequence.name} copy"
    name = base
    counter = 2
    while FollowupSequence.objects.filter(organization=sequence.organization, name__iexact=name).exists():
        name = f"{base} {counter}"
        counter += 1
    with transaction.atomic():
        copied = FollowupSequence.objects.create(
            organization=sequence.organization,
            created_by=created_by,
            name=name,
            description=sequence.description,
            whatsapp_account=sequence.whatsapp_account,
            is_active=sequence.is_active,
        )
        for step in sequence.steps.order_by("position", "created_at"):
            FollowupStep.objects.create(
                sequence=copied,
                position=step.position,
                step_type=step.step_type,
                title=step.title,
                whatsapp_template=step.whatsapp_template,
                email_subject=step.email_subject,
                email_body=step.email_body,
                reminder_text=step.reminder_text,
                schedule_type=step.schedule_type,
                delay_value=step.delay_value,
                delay_unit=step.delay_unit,
                specific_time=step.specific_time,
                specific_weekday=step.specific_weekday,
                recurring_every=step.recurring_every,
                recurring_unit=step.recurring_unit,
                retry_count=step.retry_count,
                retry_delay_hours=step.retry_delay_hours,
                is_active=step.is_active,
            )
    return copied


def delete_sequence(*, sequence):
    if sequence.lead_states.filter(
        status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED]
    ).exists():
        raise FollowupError("This sequence is assigned to active leads. Clear or change those assignments first.")
    sequence.delete()


def _next_position(sequence):
    value = sequence.steps.aggregate(max_position=Max("position"))["max_position"] or 0
    return value + 1


def _validate_schedule(
    *,
    schedule_type,
    delay_value=None,
    delay_unit="",
    specific_time=None,
    specific_weekday=None,
):
    if schedule_type not in FollowupStep.ScheduleType.values:
        raise FollowupError("Invalid delivery schedule.")
    if schedule_type == FollowupStep.ScheduleType.DELAY:
        if not delay_value or delay_value < 1:
            raise FollowupError("Enter a delay greater than zero.")
        if delay_unit not in FollowupStep.DelayUnit.values:
            raise FollowupError("Choose minutes, hours, or days for the delay.")
    if schedule_type == FollowupStep.ScheduleType.SPECIFIC_TIME and specific_time is None:
        raise FollowupError("Choose a specific delivery time.")
    if schedule_type == FollowupStep.ScheduleType.RECURRING:
        raise FollowupError(
            "Recurring steps are reserved in the data model but are not enabled until their execution rule is finalized."
        )
    if specific_weekday is not None and specific_weekday not in FollowupStep.Weekday.values:
        raise FollowupError("Invalid weekday.")


def add_whatsapp_step(
    *,
    sequence,
    template,
    schedule_type,
    delay_value=None,
    delay_unit="",
    specific_time=None,
    specific_weekday=None,
    retry_count=0,
):
    if template.organization_id != sequence.organization_id:
        raise FollowupError("Template belongs to another organization.")
    if template.account_id != sequence.whatsapp_account_id:
        raise FollowupError("Template must belong to the WhatsApp API number selected for this sequence.")
    if template.status != WhatsAppTemplate.Status.APPROVED:
        raise FollowupError("Only Meta-approved WhatsApp templates can be added.")
    try:
        retry_count = int(retry_count)
    except (TypeError, ValueError) as exc:
        raise FollowupError("Retry count must be a number from 0 to 5.") from exc
    if retry_count < 0 or retry_count > 5:
        raise FollowupError("Message Retry Count can be from 0 to 5.")
    _validate_schedule(
        schedule_type=schedule_type,
        delay_value=delay_value,
        delay_unit=delay_unit,
        specific_time=specific_time,
        specific_weekday=specific_weekday,
    )
    return FollowupStep.objects.create(
        sequence=sequence,
        position=_next_position(sequence),
        step_type=FollowupStep.StepType.WHATSAPP,
        title=template.name,
        whatsapp_template=template,
        schedule_type=schedule_type,
        delay_value=delay_value,
        delay_unit=delay_unit,
        specific_time=specific_time,
        specific_weekday=specific_weekday,
        retry_count=retry_count,
        retry_delay_hours=24,
    )


def add_email_step(
    *,
    sequence,
    title,
    subject,
    body,
    schedule_type,
    delay_value=None,
    delay_unit="",
    specific_time=None,
    specific_weekday=None,
):
    title = (title or "").strip() or f"Email {sequence.steps.count() + 1}"
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        raise FollowupError("Email subject and content are required.")
    _validate_schedule(
        schedule_type=schedule_type,
        delay_value=delay_value,
        delay_unit=delay_unit,
        specific_time=specific_time,
        specific_weekday=specific_weekday,
    )
    return FollowupStep.objects.create(
        sequence=sequence,
        position=_next_position(sequence),
        step_type=FollowupStep.StepType.EMAIL,
        title=title,
        email_subject=subject,
        email_body=body,
        schedule_type=schedule_type,
        delay_value=delay_value,
        delay_unit=delay_unit,
        specific_time=specific_time,
        specific_weekday=specific_weekday,
    )


def add_reminder_step(
    *,
    sequence,
    text,
    schedule_type,
    delay_value=None,
    delay_unit="",
    specific_time=None,
    specific_weekday=None,
):
    text = (text or "").strip()
    if not text:
        raise FollowupError("Follow-up reminder note is required.")
    _validate_schedule(
        schedule_type=schedule_type,
        delay_value=delay_value,
        delay_unit=delay_unit,
        specific_time=specific_time,
        specific_weekday=specific_weekday,
    )
    return FollowupStep.objects.create(
        sequence=sequence,
        position=_next_position(sequence),
        step_type=FollowupStep.StepType.REMINDER,
        title=(
            "Follow-Up Reminder "
            f"{sequence.steps.filter(step_type=FollowupStep.StepType.REMINDER).count() + 1}"
        ),
        reminder_text=text,
        schedule_type=schedule_type,
        delay_value=delay_value,
        delay_unit=delay_unit,
        specific_time=specific_time,
        specific_weekday=specific_weekday,
    )


def delete_step(*, step):
    sequence = step.sequence
    with transaction.atomic():
        step.delete()
        for position, item in enumerate(
            sequence.steps.order_by("position", "created_at"),
            start=1,
        ):
            if item.position != position:
                FollowupStep.objects.filter(id=item.id).update(position=position)
        _recalculate_active_states(sequence)


def _org_zone(organization):
    try:
        return ZoneInfo(organization.timezone or settings.TIME_ZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(settings.TIME_ZONE)


def _delay_delta(value, unit):
    value = int(value or 0)
    if unit in {
        FollowupStep.DelayUnit.MINUTES,
        AutoFollowupSettings.DelayUnit.MINUTES,
    }:
        return timedelta(minutes=value)
    if unit in {
        FollowupStep.DelayUnit.DAYS,
        AutoFollowupSettings.DelayUnit.DAYS,
    }:
        return timedelta(days=value)
    return timedelta(hours=value)


def _weekday_python_to_model(value):
    # Python Monday=0; SHVYA model Sunday=0.
    return (value + 1) % 7


def _specific_due(*, organization, reference, clock_time, weekday=None):
    zone = _org_zone(organization)
    local_ref = reference.astimezone(zone)
    candidate = datetime.combine(local_ref.date(), clock_time, tzinfo=zone)
    if weekday is None:
        if candidate <= local_ref:
            candidate += timedelta(days=1)
    else:
        for _ in range(8):
            if _weekday_python_to_model(candidate.weekday()) == weekday and candidate > local_ref:
                break
            candidate += timedelta(days=1)
    return candidate.astimezone(datetime_timezone.utc)


def calculate_step_due(*, step, reference, organization):
    if step.schedule_type == FollowupStep.ScheduleType.IMMEDIATE:
        return reference
    if step.schedule_type == FollowupStep.ScheduleType.DELAY:
        return reference + _delay_delta(step.delay_value, step.delay_unit)
    if step.schedule_type == FollowupStep.ScheduleType.SPECIFIC_TIME:
        return _specific_due(
            organization=organization,
            reference=reference,
            clock_time=step.specific_time,
            weekday=step.specific_weekday,
        )
    raise FollowupError("Recurring follow-up execution is not enabled yet.")


def _move_into_business_hours(*, organization, due):
    config = get_auto_followup_settings(organization)
    zone = _org_zone(organization)
    local_due = due.astimezone(zone)
    start = datetime.combine(local_due.date(), config.business_hours_start, tzinfo=zone)
    end = datetime.combine(local_due.date(), config.business_hours_end, tzinfo=zone)
    if local_due < start:
        return start.astimezone(datetime_timezone.utc)
    if local_due >= end:
        next_start = datetime.combine(
            local_due.date() + timedelta(days=1),
            config.business_hours_start,
            tzinfo=zone,
        )
        return next_start.astimezone(datetime_timezone.utc)
    return due


def _next_step_for_state(state):
    return (
        state.sequence.steps.filter(
            is_active=True,
            position__gt=state.last_completed_position,
        )
        .order_by("position", "created_at")
        .first()
    )


def _set_next_step(state, *, reference=None):
    next_step = _next_step_for_state(state)
    if next_step is None:
        state.next_step = None
        state.upcoming_send_at = None
        state.status = LeadSequenceState.Status.COMPLETED
        state.completed_at = timezone.now()
        state.save(
            update_fields=[
                "next_step",
                "upcoming_send_at",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
        return state
    reference = reference or timezone.now()
    due = calculate_step_due(
        step=next_step,
        reference=reference,
        organization=state.organization,
    )
    due = _move_into_business_hours(organization=state.organization, due=due)
    if state.paused_until and due < state.paused_until:
        due = state.paused_until
    state.next_step = next_step
    state.upcoming_send_at = due
    state.status = LeadSequenceState.Status.ACTIVE
    state.completed_at = None
    state.save(
        update_fields=[
            "next_step",
            "upcoming_send_at",
            "status",
            "completed_at",
            "updated_at",
        ]
    )
    return state


def _recalculate_active_states(sequence):
    for state in sequence.lead_states.filter(
        status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED]
    ):
        _set_next_step(state, reference=timezone.now())


def _digits(value):
    return re.sub(r"\D", "", value or "")


def _validate_lead_sender(lead, sequence):
    account = sequence.whatsapp_account
    if account.organization_id != lead.organization_id:
        raise FollowupError("The sequence WhatsApp sender belongs to another organization.")
    if account.status != WhatsAppAccount.Status.CONNECTED or not account.is_active:
        raise FollowupError("The sequence WhatsApp API number is not currently connected.")

    pipeline_number = getattr(lead.pipeline, "phone_number", "") if lead.pipeline_id else ""
    if pipeline_number:
        pipeline_digits = _digits(pipeline_number)
        account_digits = _digits(account.display_phone_number)
        # phone_number_id is a Meta object ID, not a phone number, so compare
        # against it only when the pipeline value exactly matches that ID.
        id_match = pipeline_number == account.phone_number_id
        phone_match = bool(pipeline_digits and account_digits and pipeline_digits == account_digits)
        if not phone_match and not id_match:
            raise FollowupError(
                "This lead's pipeline is mapped to a different WhatsApp API number."
            )


@transaction.atomic
def assign_sequence(*, lead, sequence, actor=None):
    if lead.organization_id != sequence.organization_id:
        raise FollowupError("Lead and sequence must belong to the same organization.")
    if not sequence.is_active:
        raise FollowupError("This sequence is inactive.")
    _validate_lead_sender(lead, sequence)

    LeadSequenceState.objects.select_for_update().filter(
        lead=lead,
        status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
    ).exclude(sequence=sequence).update(
        status=LeadSequenceState.Status.INACTIVE,
        upcoming_send_at=None,
        next_step=None,
        paused_until=None,
    )

    state, _ = LeadSequenceState.objects.select_for_update().get_or_create(
        lead=lead,
        sequence=sequence,
        defaults={
            "organization": lead.organization,
            "activated_at": timezone.now(),
        },
    )
    state.organization = lead.organization
    state.status = LeadSequenceState.Status.ACTIVE
    state.lead_auto_followup_enabled = True
    state.activated_at = timezone.now()
    state.cleared_at = None
    state.paused_until = None
    state.save()
    _set_next_step(state, reference=timezone.now())
    return state


@transaction.atomic
def clear_sequence(*, lead):
    states = LeadSequenceState.objects.select_for_update().filter(
        lead=lead,
        status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
    )
    now = timezone.now()
    states.update(
        status=LeadSequenceState.Status.CLEARED,
        cleared_at=now,
        next_step=None,
        upcoming_send_at=None,
        paused_until=None,
    )


def set_lead_followup_enabled(*, lead, enabled):
    state = LeadSequenceState.objects.filter(
        lead=lead,
        status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
    ).first()
    if not state:
        raise FollowupError("This lead does not have an active Auto Follow-up sequence.")
    state.lead_auto_followup_enabled = bool(enabled)
    state.save(update_fields=["lead_auto_followup_enabled", "updated_at"])
    return state


def _conversation_delay(config):
    return _delay_delta(config.conversation_delay_value, config.conversation_delay_unit)


def register_lead_reply(*, lead, at=None):
    """Delay, but do not clear, the active sequence after a lead reply."""
    at = at or timezone.now()
    state = LeadSequenceState.objects.filter(
        lead=lead,
        status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
    ).first()
    if not state:
        return None
    config = get_auto_followup_settings(lead.organization)
    state.last_inbound_at = at
    state.paused_until = at + _conversation_delay(config)
    if state.upcoming_send_at is None or state.upcoming_send_at < state.paused_until:
        state.upcoming_send_at = state.paused_until
    state.save(
        update_fields=[
            "last_inbound_at",
            "paused_until",
            "upcoming_send_at",
            "updated_at",
        ]
    )
    return state


def register_manual_outbound(*, lead, at=None):
    """Organization replies keep the sequence but protect conversation space."""
    at = at or timezone.now()
    state = LeadSequenceState.objects.filter(
        lead=lead,
        status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
    ).first()
    if not state:
        return None
    config = get_auto_followup_settings(lead.organization)
    state.last_manual_outbound_at = at
    state.paused_until = at + _conversation_delay(config)
    if state.upcoming_send_at is None or state.upcoming_send_at < state.paused_until:
        state.upcoming_send_at = state.paused_until
    state.save(
        update_fields=[
            "last_manual_outbound_at",
            "paused_until",
            "upcoming_send_at",
            "updated_at",
        ]
    )
    return state


def _lead_template_values(lead, user=None):
    values = {
        "lead_name": lead.name or "",
        "lead_first_name": (lead.name or "").split(" ")[0],
        "phone": lead.phone or "",
        "email": lead.email or "",
        "lead_source": getattr(lead, "lead_source", "") or "",
        "org_name": lead.organization.name,
        "user_name": getattr(user, "name", "") or getattr(user, "email", "") or "",
        "pipeline_name": lead.pipeline.name if lead.pipeline_id else "",
        "stage_name": lead.stage.name if lead.stage_id else "",
    }
    values.update(getattr(lead, "attributes", None) or {})
    return values


def _template_components(template, lead, user=None):
    metadata = WhatsAppTemplateMetadata.objects.filter(template=template).first()
    mapping = (metadata.placeholder_mapping if metadata else {}) or {}
    if not mapping:
        return []
    values = _lead_template_values(lead, user=user)
    parameters = []
    for _, key in sorted(mapping.items(), key=lambda item: int(item[0])):
        value = values.get(key, "")
        parameters.append({"type": "text", "text": str(value or "")})
    return [{"type": "body", "parameters": parameters}] if parameters else []


def _create_execution(state, step):
    pending = (
        state.executions.filter(step=step, status=FollowupExecution.Status.PENDING)
        .order_by("-created_at")
        .first()
    )
    if pending:
        pending.status = FollowupExecution.Status.PROCESSING
        pending.started_at = timezone.now()
        pending.save(update_fields=["status", "started_at", "updated_at"])
        return pending

    previous = (
        state.executions.filter(step=step).aggregate(max_attempt=Max("attempt_no"))["max_attempt"]
        or 0
    )
    return FollowupExecution.objects.create(
        organization=state.organization,
        state=state,
        lead=state.lead,
        sequence=state.sequence,
        step=step,
        scheduled_for=state.upcoming_send_at or timezone.now(),
        status=FollowupExecution.Status.PROCESSING,
        attempt_no=previous + 1,
        max_attempts=1 + step.retry_count,
        started_at=timezone.now(),
    )


def _mark_skipped_and_advance(state, execution, reason):
    now = timezone.now()
    execution.status = FollowupExecution.Status.SKIPPED
    execution.error = reason
    execution.finished_at = now
    execution.save(update_fields=["status", "error", "finished_at", "updated_at"])
    state.last_completed_position = execution.step.position
    state.last_step_completed_at = now
    state.save(update_fields=["last_completed_position", "last_step_completed_at", "updated_at"])
    _set_next_step(state, reference=now)


def _handle_failure(state, execution, exc):
    now = timezone.now()
    if execution.attempt_no < execution.max_attempts:
        retry_at = now + timedelta(hours=execution.step.retry_delay_hours or 24)
        execution.status = FollowupExecution.Status.RETRY_WAIT
        execution.error = str(exc)
        execution.next_retry_at = retry_at
        execution.finished_at = now
        execution.save(
            update_fields=[
                "status",
                "error",
                "next_retry_at",
                "finished_at",
                "updated_at",
            ]
        )
        state.upcoming_send_at = _move_into_business_hours(
            organization=state.organization,
            due=retry_at,
        )
        state.save(update_fields=["upcoming_send_at", "updated_at"])
        return
    execution.status = FollowupExecution.Status.FAILED
    execution.error = str(exc)
    execution.finished_at = now
    execution.save(update_fields=["status", "error", "finished_at", "updated_at"])
    state.status = LeadSequenceState.Status.PAUSED
    state.upcoming_send_at = None
    state.save(update_fields=["status", "upcoming_send_at", "updated_at"])


def _send_whatsapp_step(state, step, execution):
    lead = state.lead
    account = state.sequence.whatsapp_account
    template = step.whatsapp_template
    if not lead.phone:
        _mark_skipped_and_advance(state, execution, "Lead has no phone number.")
        return
    if account.status != WhatsAppAccount.Status.CONNECTED or not account.is_active:
        raise FollowupError("The sequence WhatsApp API number is not connected.")
    if not template or template.status != WhatsAppTemplate.Status.APPROVED:
        raise FollowupError("The selected WhatsApp template is no longer approved.")
    if template.account_id != account.id:
        raise FollowupError("Template and sequence WhatsApp account no longer match.")

    sender, _ = FollowupSenderState.objects.select_for_update().get_or_create(account=account)
    now = timezone.now()
    if sender.next_available_at and sender.next_available_at > now:
        execution.status = FollowupExecution.Status.PENDING
        execution.started_at = None
        execution.scheduled_for = sender.next_available_at
        execution.save(
            update_fields=["status", "started_at", "scheduled_for", "updated_at"]
        )
        state.upcoming_send_at = sender.next_available_at
        state.save(update_fields=["upcoming_send_at", "updated_at"])
        return

    body = render_template_body(
        template=template,
        lead=lead,
        user=state.sequence.created_by,
    )
    message = WhatsAppMessage.objects.create(
        organization=state.organization,
        account=account,
        lead=lead,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        from_number=account.phone_number_id,
        to_number=lead.phone,
        body=body,
        message_type=WhatsAppMessage.MessageType.TEXT,
        media_payload={},
        status=WhatsAppMessage.Status.QUEUED,
        raw_payload={
            "shvya_auto_followup": {
                "sequence_id": str(state.sequence_id),
                "step_id": str(step.id),
                "template_id": str(template.id),
                "template_name": template.name,
                "attempt": execution.attempt_no,
            }
        },
    )
    execution.whatsapp_message = message
    execution.save(update_fields=["whatsapp_message", "updated_at"])

    metadata = WhatsAppTemplateMetadata.objects.filter(template=template).first()
    language = metadata.language if metadata and metadata.language else "en_US"
    client = WhatsAppClient(
        phone_number_id=account.phone_number_id,
        access_token=account.access_token,
    )
    try:
        response = client.send_template_message(
            to=lead.phone,
            template_name=template.name,
            language_code=language,
            components=_template_components(
                template,
                lead,
                user=state.sequence.created_by,
            ),
        )
    except WhatsAppAPIError as exc:
        message.status = WhatsAppMessage.Status.FAILED
        message.error = str(exc)
        message.save(update_fields=["status", "error", "updated_at"])
        raise FollowupError(str(exc)) from exc

    meta_messages = response.get("messages") or []
    message.external_id = meta_messages[0].get("id") if meta_messages else None
    message.status = WhatsAppMessage.Status.SENT
    message.raw_payload = {
        "meta": response,
        "shvya_auto_followup": {
            "sequence_id": str(state.sequence_id),
            "step_id": str(step.id),
            "template_id": str(template.id),
            "template_name": template.name,
            "attempt": execution.attempt_no,
        },
    }
    message.save(
        update_fields=["external_id", "status", "raw_payload", "updated_at"]
    )

    finished = timezone.now()
    execution.status = FollowupExecution.Status.SENT
    execution.finished_at = finished
    execution.payload = {
        "meta_message_id": message.external_id or "",
        "template": template.name,
    }
    execution.save(update_fields=["status", "finished_at", "payload", "updated_at"])

    sender.last_sent_at = finished
    sender.next_available_at = finished + timedelta(seconds=WHATSAPP_MIN_SEND_GAP_SECONDS)
    sender.last_lead = lead
    sender.save(
        update_fields=[
            "last_sent_at",
            "next_available_at",
            "last_lead",
            "updated_at",
        ]
    )

    state.last_sent_at = finished
    state.last_completed_position = step.position
    state.last_step_completed_at = finished
    state.paused_until = None
    state.save(
        update_fields=[
            "last_sent_at",
            "last_completed_position",
            "last_step_completed_at",
            "paused_until",
            "updated_at",
        ]
    )
    _set_next_step(state, reference=finished)


def _render_text(text, lead, user=None):
    values = _lead_template_values(lead, user=user)
    rendered = text or ""
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", str(value or ""))
    return rendered


def _send_email_step(state, step, execution):
    lead = state.lead
    if not lead.email:
        _mark_skipped_and_advance(state, execution, "Lead has no email address.")
        return
    if not getattr(settings, "FOLLOWUP_EMAIL_DELIVERY_ENABLED", False):
        execution.status = FollowupExecution.Status.BLOCKED
        execution.error = (
            "Email transport is prepared but disabled until organization "
            "DNS/sender configuration is ready."
        )
        execution.finished_at = timezone.now()
        execution.save(update_fields=["status", "error", "finished_at", "updated_at"])
        state.status = LeadSequenceState.Status.PAUSED
        state.upcoming_send_at = None
        state.save(update_fields=["status", "upcoming_send_at", "updated_at"])
        return

    subject = _render_text(
        step.email_subject,
        lead,
        user=state.sequence.created_by,
    )
    body = _render_text(
        step.email_body,
        lead,
        user=state.sequence.created_by,
    )
    email = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[lead.email],
    )
    email.send(fail_silently=False)
    now = timezone.now()
    execution.status = FollowupExecution.Status.SENT
    execution.finished_at = now
    execution.payload = {"recipient": lead.email, "subject": subject}
    execution.save(update_fields=["status", "finished_at", "payload", "updated_at"])
    state.last_sent_at = now
    state.last_completed_position = step.position
    state.last_step_completed_at = now
    state.save(
        update_fields=[
            "last_sent_at",
            "last_completed_position",
            "last_step_completed_at",
            "updated_at",
        ]
    )
    _set_next_step(state, reference=now)


def _create_reminder_step(state, step, execution):
    lead = state.lead
    now = timezone.now()
    LeadReminder.objects.filter(lead=lead, status="pending").update(status="cancelled")
    assignee = getattr(lead.pipeline, "owner", None) if lead.pipeline_id else None
    assignee = assignee or state.sequence.created_by
    reminder = LeadReminder.objects.create(
        lead=lead,
        assigned_to=assignee,
        title="Auto Follow-up reminder",
        description=_render_text(
            step.reminder_text,
            lead,
            user=state.sequence.created_by,
        ),
        due_at=now,
        status="pending",
    )
    execution.reminder = reminder
    execution.status = FollowupExecution.Status.CREATED
    execution.finished_at = now
    execution.payload = {"reminder_id": str(reminder.id)}
    execution.save(
        update_fields=["reminder", "status", "finished_at", "payload", "updated_at"]
    )
    state.last_completed_position = step.position
    state.last_step_completed_at = now
    state.save(update_fields=["last_completed_position", "last_step_completed_at", "updated_at"])
    _set_next_step(state, reference=now)


@transaction.atomic
def process_due_state(state_id):
    state = (
        LeadSequenceState.objects.select_for_update()
        .select_related(
            "organization",
            "lead",
            "lead__pipeline",
            "lead__stage",
            "sequence",
            "sequence__whatsapp_account",
            "sequence__created_by",
            "next_step",
            "next_step__whatsapp_template",
        )
        .filter(id=state_id)
        .first()
    )
    if not state or state.status != LeadSequenceState.Status.ACTIVE:
        return False
    if not state.lead_auto_followup_enabled or not state.sequence.is_active:
        return False
    config = get_auto_followup_settings(state.organization)
    if not config.enabled:
        return False
    now = timezone.now()
    if state.paused_until and state.paused_until > now:
        state.upcoming_send_at = state.paused_until
        state.save(update_fields=["upcoming_send_at", "updated_at"])
        return False
    if not state.next_step:
        _set_next_step(state, reference=now)
        return False
    if state.upcoming_send_at and state.upcoming_send_at > now:
        return False

    adjusted = _move_into_business_hours(organization=state.organization, due=now)
    if adjusted > now:
        state.upcoming_send_at = adjusted
        state.save(update_fields=["upcoming_send_at", "updated_at"])
        return False

    step = state.next_step
    execution = _create_execution(state, step)
    try:
        if step.step_type == FollowupStep.StepType.WHATSAPP:
            _send_whatsapp_step(state, step, execution)
        elif step.step_type == FollowupStep.StepType.EMAIL:
            _send_email_step(state, step, execution)
        elif step.step_type == FollowupStep.StepType.REMINDER:
            _create_reminder_step(state, step, execution)
        else:
            raise FollowupError("Unsupported Auto Follow-up step type.")
    except Exception as exc:
        _handle_failure(state, execution, exc)
        return False
    return True


def dispatch_one_due_state():
    """Process at most one due lead per invocation, preventing fan-out bursts."""
    if not cache.add(DISPATCH_LOCK_KEY, "1", timeout=DISPATCH_LOCK_SECONDS):
        return {"status": "locked"}
    try:
        now = timezone.now()
        state_id = (
            LeadSequenceState.objects.filter(
                status=LeadSequenceState.Status.ACTIVE,
                lead_auto_followup_enabled=True,
                sequence__is_active=True,
                upcoming_send_at__isnull=False,
                upcoming_send_at__lte=now,
                organization__auto_followup_settings__enabled=True,
            )
            .order_by("upcoming_send_at", "assigned_at")
            .values_list("id", flat=True)
            .first()
        )
        if not state_id:
            return {"status": "idle"}
        processed = process_due_state(state_id)
        return {
            "status": "processed" if processed else "deferred",
            "state_id": str(state_id),
        }
    finally:
        cache.delete(DISPATCH_LOCK_KEY)
