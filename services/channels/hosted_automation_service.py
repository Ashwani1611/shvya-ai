"""Hosted WhatsApp automation rules.

This module intentionally keeps the linked-device hard rules separate from the
Meta Cloud API follow-up path. Hosted accounts use free-form WhatsApp content,
a delayed AI queue, stricter sender pacing, durable duplicate protection, and
an account-health circuit breaker.
"""

from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from urllib.parse import quote

from decouple import config
from django.core.cache import cache
from django.core.signing import TimestampSigner
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.followups.models import (
    FollowupExecution,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from apps.hosted_automation.models import (
    HostedAccountHealth,
    HostedAutomationJob,
    HostedFollowupStepConfig,
)


HOSTED_CONNECTION_TYPE = "hosted"
AI_RESPONSE_DELAY_SECONDS = 60
HOSTED_ENGINE_INTERVAL_SECONDS = 10
SAME_CONTENT_GAP_SECONDS = 180
DIFFERENT_CONTENT_GAP_SECONDS = 90
RECOMMENDED_MESSAGING_LIMIT = 250
HEALTH_COOLDOWN = timedelta(hours=12)
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MEDIA_TOKEN_MAX_AGE_SECONDS = 15 * 60

HOSTED_AI_LOCK = "shvya:hosted-ai:dispatcher"
HOSTED_FOLLOWUP_LOCK = "shvya:hosted-followup:dispatcher"
API_FOLLOWUP_LOCK = "shvya:api-followup:dispatcher"

ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".txt",
    ".jpg",
    ".jpeg",
    ".png",
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".mp4",
    ".mov",
    ".webm",
    ".3gp",
}


class HostedAutomationError(ValueError):
    pass


class HostedAutomationPaused(HostedAutomationError):
    def __init__(self, paused_until):
        self.paused_until = paused_until
        super().__init__(f"Hosted automation is paused until {paused_until.isoformat()}.")


def _content_hash(body: str) -> str:
    normalized = str(body or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _reset_expired_health(health: HostedAccountHealth, *, now=None) -> bool:
    now = now or timezone.now()
    if health.paused_until and health.paused_until <= now:
        health.paused_until = None
        health.window_messages_sent = 0
        health.window_started_at = now
        return True
    return False


@transaction.atomic
def get_or_create_health(*, account) -> HostedAccountHealth:
    if account.connection_type != HOSTED_CONNECTION_TYPE:
        raise HostedAutomationError("Account Health is available only for Hosted Accounts.")
    now = timezone.now()
    health, _created = HostedAccountHealth.objects.select_for_update().get_or_create(
        account=account,
        defaults={"window_started_at": now},
    )
    if _reset_expired_health(health, now=now):
        health.save(
            update_fields=[
                "paused_until",
                "window_messages_sent",
                "window_started_at",
                "updated_at",
            ]
        )
    return health


def health_snapshot(*, account):
    health = get_or_create_health(account=account)
    now = timezone.now()
    paused = bool(health.enabled and health.paused_until and health.paused_until > now)
    return {
        "enabled": health.enabled,
        "total_messages_sent": health.total_messages_sent,
        "window_messages_sent": health.window_messages_sent,
        "recommended_limit": RECOMMENDED_MESSAGING_LIMIT,
        "paused": paused,
        "paused_until": health.paused_until,
        "remaining": max(0, RECOMMENDED_MESSAGING_LIMIT - health.window_messages_sent),
    }


@transaction.atomic
def set_health_enabled(*, account, enabled):
    health = get_or_create_health(account=account)
    health = HostedAccountHealth.objects.select_for_update().get(pk=health.pk)
    now = timezone.now()
    health.enabled = bool(enabled)
    if not health.enabled:
        health.paused_until = None
    elif health.window_messages_sent >= RECOMMENDED_MESSAGING_LIMIT:
        health.paused_until = now + HEALTH_COOLDOWN
    health.save(update_fields=["enabled", "paused_until", "updated_at"])
    return health_snapshot(account=account)


def automation_pause_until(*, account):
    health = get_or_create_health(account=account)
    if health.enabled and health.paused_until and health.paused_until > timezone.now():
        return health.paused_until
    return None


def message_is_automation(message) -> bool:
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    return bool(payload.get("shvya_ai") or payload.get("shvya_auto_followup"))


@transaction.atomic
def record_hosted_send(*, account, message=None):
    """Count one successfully sent hosted message exactly once at send time."""
    if account.connection_type != HOSTED_CONNECTION_TYPE:
        return None
    health = get_or_create_health(account=account)
    health = HostedAccountHealth.objects.select_for_update().get(pk=health.pk)
    now = timezone.now()
    if _reset_expired_health(health, now=now):
        pass
    if not health.window_started_at:
        health.window_started_at = now
    health.total_messages_sent += 1
    health.window_messages_sent += 1

    payload = message.raw_payload if message and isinstance(message.raw_payload, dict) else {}
    followup = payload.get("shvya_auto_followup") or {}
    content_hash = str(followup.get("content_hash") or "")
    if content_hash:
        health.last_followup_sent_at = now
        health.last_followup_content_hash = content_hash

    if (
        health.enabled
        and health.window_messages_sent >= RECOMMENDED_MESSAGING_LIMIT
        and not (health.paused_until and health.paused_until > now)
    ):
        health.paused_until = now + HEALTH_COOLDOWN

    health.save()
    return health


def validate_hosted_attachment(upload):
    if upload is None:
        return
    if upload.size > MAX_ATTACHMENT_BYTES:
        raise HostedAutomationError("WhatsApp attachments can be at most 50 MB.")
    extension = Path(upload.name or "").suffix.lower()
    if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise HostedAutomationError(
            "Unsupported file. Use PDF, DOC/DOCX, TXT, JPEG/JPG, PNG, voice/audio, or video files."
        )


def _message_type_for_attachment(name: str):
    extension = Path(name or "").suffix.lower()
    if extension in {".jpg", ".jpeg", ".png"}:
        return WhatsAppMessage.MessageType.IMAGE
    if extension in {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}:
        return WhatsAppMessage.MessageType.AUDIO
    if extension in {".mp4", ".mov", ".webm", ".3gp"}:
        return WhatsAppMessage.MessageType.VIDEO
    return WhatsAppMessage.MessageType.DOCUMENT


def _next_position(sequence):
    value = sequence.steps.order_by("-position").values_list("position", flat=True).first() or 0
    return value + 1


@transaction.atomic
def add_hosted_whatsapp_step(
    *,
    sequence,
    title,
    body,
    attachment=None,
    schedule_type,
    delay_value=None,
    delay_unit="",
    specific_time=None,
    specific_weekday=None,
    recurring_every=None,
    recurring_unit="",
    recurring_weekdays=None,
):
    from services.followup_service import _validate_schedule

    if sequence.whatsapp_account.connection_type != HOSTED_CONNECTION_TYPE:
        raise HostedAutomationError("Use WhatsApp is available only for Hosted Account sequences.")
    title = str(title or "").strip()
    body = str(body or "").strip()
    if not title:
        raise HostedAutomationError("Message Name is required.")
    if len(title) > 255:
        raise HostedAutomationError("Message Name must be 255 characters or fewer.")
    if not body:
        raise HostedAutomationError("Message content is required.")
    validate_hosted_attachment(attachment)
    _validate_schedule(
        schedule_type=schedule_type,
        delay_value=delay_value,
        delay_unit=delay_unit,
        specific_time=specific_time,
        specific_weekday=specific_weekday,
        recurring_every=recurring_every,
        recurring_unit=recurring_unit,
        recurring_weekdays=recurring_weekdays,
    )
    step = FollowupStep.objects.create(
        sequence=sequence,
        position=_next_position(sequence),
        step_type=FollowupStep.StepType.WHATSAPP,
        title=title,
        whatsapp_template=None,
        schedule_type=schedule_type,
        delay_value=delay_value,
        delay_unit=delay_unit,
        specific_time=specific_time,
        specific_weekday=specific_weekday,
        recurring_every=recurring_every,
        recurring_unit=recurring_unit,
        recurring_weekdays=list(recurring_weekdays or []),
        retry_count=0,
    )
    mime_type = ""
    if attachment:
        mime_type = getattr(attachment, "content_type", "") or mimetypes.guess_type(attachment.name)[0] or ""
    HostedFollowupStepConfig.objects.create(
        step=step,
        body=body,
        attachment=attachment or "",
        attachment_original_name=(attachment.name if attachment else "")[:255],
        attachment_mime_type=mime_type[:120],
        attachment_size=(attachment.size if attachment else 0),
        authored_content_hash=_content_hash(body),
    )
    return step


@transaction.atomic
def update_hosted_whatsapp_step(*, step, title, body, attachment=None, remove_attachment=False):
    if step.sequence.whatsapp_account.connection_type != HOSTED_CONNECTION_TYPE:
        raise HostedAutomationError("This is not a Hosted Account WhatsApp step.")
    title = str(title or "").strip()
    body = str(body or "").strip()
    if not title or not body:
        raise HostedAutomationError("Message Name and content are required.")
    validate_hosted_attachment(attachment)
    step.title = title[:255]
    step.save(update_fields=["title", "updated_at"])
    hosted, _ = HostedFollowupStepConfig.objects.get_or_create(
        step=step,
        defaults={"body": body, "authored_content_hash": _content_hash(body)},
    )
    hosted.body = body
    hosted.authored_content_hash = _content_hash(body)
    if remove_attachment:
        hosted.attachment = ""
        hosted.attachment_original_name = ""
        hosted.attachment_mime_type = ""
        hosted.attachment_size = 0
    if attachment:
        hosted.attachment = attachment
        hosted.attachment_original_name = attachment.name[:255]
        hosted.attachment_mime_type = (
            getattr(attachment, "content_type", "")
            or mimetypes.guess_type(attachment.name)[0]
            or ""
        )[:120]
        hosted.attachment_size = attachment.size
    hosted.save()
    return hosted


def duplicate_hosted_configs(*, source_sequence, copied_sequence):
    if source_sequence.whatsapp_account.connection_type != HOSTED_CONNECTION_TYPE:
        return copied_sequence
    source_steps = list(source_sequence.steps.order_by("position", "created_at"))
    copied_steps = list(copied_sequence.steps.order_by("position", "created_at"))
    for source_step, copied_step in zip(source_steps, copied_steps):
        try:
            source_config = source_step.hosted_config
        except HostedFollowupStepConfig.DoesNotExist:
            continue
        HostedFollowupStepConfig.objects.create(
            step=copied_step,
            body=source_config.body,
            attachment=source_config.attachment.name if source_config.attachment else "",
            attachment_original_name=source_config.attachment_original_name,
            attachment_mime_type=source_config.attachment_mime_type,
            attachment_size=source_config.attachment_size,
            authored_content_hash=source_config.authored_content_hash,
        )
    return copied_sequence


def _lead_values(lead, user=None):
    from services.followup_service import _lead_template_values

    return _lead_template_values(lead, user=user)


def render_hosted_content(*, body, lead, user=None):
    rendered = str(body or "")
    for key, value in _lead_values(lead, user=user).items():
        text = str(value or "")
        rendered = rendered.replace("{{" + key + "}}", text)
        rendered = rendered.replace("{" + key + "}", text)
    return rendered


def media_url_for_config(hosted_config):
    if not hosted_config.attachment:
        return None
    token = TimestampSigner(salt="hosted-followup-media").sign(str(hosted_config.id))
    path = reverse("whatsapp-hosted-automation-media", args=[hosted_config.id])
    base = config("WHATSAPP_HOSTED_INTERNAL_BASE_URL", default="http://web:8000").rstrip("/")
    return f"{base}{path}?token={quote(token, safe='')}"


@transaction.atomic
def enqueue_ai_engagement(*, account, lead, source_message):
    if account.connection_type != HOSTED_CONNECTION_TYPE:
        return None
    available_at = (source_message.created_at or timezone.now()) + timedelta(
        seconds=AI_RESPONSE_DELAY_SECONDS
    )
    job, _created = HostedAutomationJob.objects.get_or_create(
        source_message=source_message,
        defaults={
            "organization": account.organization,
            "account": account,
            "lead": lead,
            "available_at": available_at,
        },
    )
    return job


def _latest_hosted_inbound(*, job):
    return (
        WhatsAppMessage.objects.filter(
            organization=job.organization,
            account=job.account,
            lead=job.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def has_pending_ai(*, account):
    return HostedAutomationJob.objects.filter(
        account=account,
        status__in=[HostedAutomationJob.Status.QUEUED, HostedAutomationJob.Status.PROCESSING],
    ).exists()


def _next_ai_time(*, account):
    return (
        HostedAutomationJob.objects.filter(
            account=account,
            status=HostedAutomationJob.Status.QUEUED,
        )
        .order_by("available_at")
        .values_list("available_at", flat=True)
        .first()
    )


def dispatch_one_hosted_ai_job():
    """Claim one due AI job. Hosted AI always gets first dispatch priority."""
    if not cache.add(HOSTED_AI_LOCK, "1", timeout=18):
        return {"status": "locked"}
    try:
        now = timezone.now()
        candidate_ids = list(
            HostedAutomationJob.objects.filter(
                status=HostedAutomationJob.Status.QUEUED,
                available_at__lte=now,
                account__connection_type=HOSTED_CONNECTION_TYPE,
                account__is_active=True,
                account__status=WhatsAppAccount.Status.CONNECTED,
            )
            .order_by("available_at", "created_at")
            .values_list("id", flat=True)[:20]
        )
        for job_id in candidate_ids:
            with transaction.atomic():
                job = (
                    HostedAutomationJob.objects.select_for_update()
                    .select_related("account", "organization", "lead", "source_message")
                    .filter(id=job_id, status=HostedAutomationJob.Status.QUEUED)
                    .first()
                )
                if not job:
                    continue
                latest = _latest_hosted_inbound(job=job)
                if not latest or latest.id != job.source_message_id:
                    job.status = HostedAutomationJob.Status.SKIPPED
                    job.completed_at = now
                    job.result = {"reason": "superseded_by_newer_lead_message"}
                    job.save(update_fields=["status", "completed_at", "result", "updated_at"])
                    continue
                from services.channels.hosted_whatsapp_service import get_session_settings

                session_settings = get_session_settings(account=job.account)
                if not session_settings.get("ai_auto_reply"):
                    job.status = HostedAutomationJob.Status.SKIPPED
                    job.completed_at = now
                    job.result = {"reason": "ai_auto_reply_disabled"}
                    job.save(update_fields=["status", "completed_at", "result", "updated_at"])
                    continue
                pause_until = automation_pause_until(account=job.account)
                if pause_until:
                    job.available_at = pause_until
                    job.save(update_fields=["available_at", "updated_at"])
                    continue
                job.status = HostedAutomationJob.Status.PROCESSING
                job.started_at = now
                job.save(update_fields=["status", "started_at", "updated_at"])
                from apps.hosted_automation.tasks import process_hosted_ai_engagement_job_task

                transaction.on_commit(
                    lambda value=str(job.id): process_hosted_ai_engagement_job_task.delay(value)
                )
                return {"status": "dispatched", "job_id": str(job.id)}
        return {"status": "idle"}
    finally:
        cache.delete(HOSTED_AI_LOCK)


def _delay_from_session_settings(settings):
    value = int(settings.get("active_conversation_delay_value") or 0)
    unit = settings.get("active_conversation_delay_unit") or "hours"
    if unit == "minutes":
        return timedelta(minutes=value)
    if unit == "days":
        return timedelta(days=value)
    return timedelta(hours=value)


@transaction.atomic
def register_hosted_lead_reply(*, account, lead, at=None):
    """Apply the Hosted Account conversation-delay setting to follow-ups."""
    at = at or timezone.now()
    state = (
        LeadSequenceState.objects.select_for_update()
        .filter(
            lead=lead,
            sequence__whatsapp_account=account,
            status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
        )
        .first()
    )
    if not state:
        return None
    from services.channels.hosted_whatsapp_service import get_session_settings

    pause_until = at + _delay_from_session_settings(get_session_settings(account=account))
    state.last_inbound_at = at
    state.paused_until = pause_until
    if state.upcoming_send_at is None or state.upcoming_send_at < pause_until:
        state.upcoming_send_at = pause_until
    state.save(
        update_fields=["last_inbound_at", "paused_until", "upcoming_send_at", "updated_at"]
    )
    return state


@transaction.atomic
def register_hosted_manual_outbound(*, account, lead, at=None):
    at = at or timezone.now()
    state = (
        LeadSequenceState.objects.select_for_update()
        .filter(
            lead=lead,
            sequence__whatsapp_account=account,
            status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
        )
        .first()
    )
    if not state:
        return None
    from services.channels.hosted_whatsapp_service import get_session_settings

    pause_until = at + _delay_from_session_settings(get_session_settings(account=account))
    state.last_manual_outbound_at = at
    state.paused_until = pause_until
    if state.upcoming_send_at is None or state.upcoming_send_at < pause_until:
        state.upcoming_send_at = pause_until
    state.save(
        update_fields=[
            "last_manual_outbound_at",
            "paused_until",
            "upcoming_send_at",
            "updated_at",
        ]
    )
    return state


def _hosted_business_hours_due(*, state, account, due):
    from services.channels.hosted_whatsapp_service import get_session_settings
    from services.followup_service import _org_zone

    settings = get_session_settings(account=account)
    try:
        start_time = datetime.strptime(settings["business_hours_start"], "%H:%M").time()
        end_time = datetime.strptime(settings["business_hours_end"], "%H:%M").time()
    except (KeyError, TypeError, ValueError):
        return due
    zone = _org_zone(state.organization)
    local_due = due.astimezone(zone)
    start = datetime.combine(local_due.date(), start_time, tzinfo=zone)
    end = datetime.combine(local_due.date(), end_time, tzinfo=zone)
    if start_time < end_time:
        if local_due < start:
            return start.astimezone(datetime_timezone.utc)
        if local_due >= end:
            return datetime.combine(
                local_due.date() + timedelta(days=1), start_time, tzinfo=zone
            ).astimezone(datetime_timezone.utc)
        return due
    # Overnight window, e.g. 20:00 to 06:00.
    if local_due.time() >= start_time or local_due.time() < end_time:
        return due
    return start.astimezone(datetime_timezone.utc)


def _defer_state(state, when):
    state.upcoming_send_at = when
    state.save(update_fields=["upcoming_send_at", "updated_at"])


def _historical_duplicate(*, state, content_hash):
    return FollowupExecution.objects.filter(
        lead=state.lead,
        status=FollowupExecution.Status.SENT,
        sequence__whatsapp_account__connection_type=HOSTED_CONNECTION_TYPE,
        payload__hosted_content_hash=content_hash,
    ).exists()


def _hosted_gap_eligible_at(*, health, content_hash, now):
    if not health.last_followup_sent_at:
        return now
    seconds = (
        SAME_CONTENT_GAP_SECONDS
        if health.last_followup_content_hash == content_hash
        else DIFFERENT_CONTENT_GAP_SECONDS
    )
    return health.last_followup_sent_at + timedelta(seconds=seconds)


@transaction.atomic
def process_hosted_due_state(state_id):
    from services.followup_service import (
        _create_execution,
        _create_reminder_step,
        _handle_failure,
        _mark_skipped_and_advance,
        _move_into_business_hours,
        _repeat_or_advance,
        _send_email_step,
        _set_next_step,
        get_auto_followup_settings,
    )

    state = (
        LeadSequenceState.objects.select_for_update(of=("self",))
        .select_related(
            "organization",
            "lead",
            "lead__pipeline",
            "lead__stage",
            "sequence",
            "sequence__whatsapp_account",
            "sequence__created_by",
            "next_step",
        )
        .filter(id=state_id)
        .first()
    )
    if not state or state.status != LeadSequenceState.Status.ACTIVE:
        return False
    account = state.sequence.whatsapp_account
    if account.connection_type != HOSTED_CONNECTION_TYPE:
        return False
    if not state.lead_auto_followup_enabled or not state.sequence.is_active:
        return False
    if not get_auto_followup_settings(state.organization).enabled:
        return False

    from services.channels.hosted_whatsapp_service import get_session_settings

    session_settings = get_session_settings(account=account)
    if not session_settings.get("auto_follow_up", True):
        _defer_state(state, timezone.now() + timedelta(minutes=5))
        return False

    now = timezone.now()
    if state.paused_until and state.paused_until > now:
        _defer_state(state, state.paused_until)
        return False
    health_pause = automation_pause_until(account=account)
    if health_pause:
        _defer_state(state, health_pause)
        return False
    if has_pending_ai(account=account):
        next_ai = _next_ai_time(account=account)
        _defer_state(
            state,
            max(now + timedelta(seconds=HOSTED_ENGINE_INTERVAL_SECONDS), (next_ai or now) + timedelta(seconds=HOSTED_ENGINE_INTERVAL_SECONDS)),
        )
        return False
    if not state.next_step:
        _set_next_step(state, reference=now)
        return False
    if state.upcoming_send_at and state.upcoming_send_at > now:
        return False

    adjusted = _move_into_business_hours(organization=state.organization, due=now)
    adjusted = _hosted_business_hours_due(state=state, account=account, due=adjusted)
    if adjusted > now:
        _defer_state(state, adjusted)
        return False

    step = state.next_step
    execution = _create_execution(state, step)
    try:
        if step.step_type == FollowupStep.StepType.EMAIL:
            _send_email_step(state, step, execution)
            return True
        if step.step_type == FollowupStep.StepType.REMINDER:
            _create_reminder_step(state, step, execution)
            return True
        if step.step_type != FollowupStep.StepType.WHATSAPP:
            raise HostedAutomationError("Unsupported Hosted Account follow-up step.")

        try:
            hosted = step.hosted_config
        except HostedFollowupStepConfig.DoesNotExist:
            _mark_skipped_and_advance(
                state, execution, "Hosted WhatsApp step has no message content."
            )
            return True

        content_hash = hosted.authored_content_hash or _content_hash(hosted.body)
        if _historical_duplicate(state=state, content_hash=content_hash):
            _mark_skipped_and_advance(
                state,
                execution,
                "Same Hosted WhatsApp follow-up content was already sent to this lead; skipped permanently.",
            )
            return True

        health = get_or_create_health(account=account)
        eligible_at = _hosted_gap_eligible_at(
            health=health, content_hash=content_hash, now=now
        )
        if eligible_at > now:
            execution.status = FollowupExecution.Status.PENDING
            execution.started_at = None
            execution.scheduled_for = eligible_at
            execution.save(
                update_fields=["status", "started_at", "scheduled_for", "updated_at"]
            )
            _defer_state(state, eligible_at)
            return False

        body = render_hosted_content(
            body=hosted.body,
            lead=state.lead,
            user=state.sequence.created_by,
        )
        if not state.lead.phone:
            _mark_skipped_and_advance(state, execution, "Lead has no phone number.")
            return True

        media_payload = {}
        message_type = WhatsAppMessage.MessageType.TEXT
        if hosted.attachment:
            media_url = media_url_for_config(hosted)
            message_type = _message_type_for_attachment(hosted.attachment_original_name)
            media_payload = {
                "source": "url",
                "url": media_url,
                "filename": hosted.attachment_original_name,
            }

        message = WhatsAppMessage.objects.create(
            organization=state.organization,
            account=account,
            lead=state.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=account.display_phone_number or account.phone_number_id,
            to_number=state.lead.phone,
            body=body,
            message_type=message_type,
            media_payload=media_payload,
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload={
                "shvya_auto_followup": {
                    "provider": "hosted",
                    "sequence_id": str(state.sequence_id),
                    "step_id": str(step.id),
                    "execution_id": str(execution.id),
                    "content_hash": content_hash,
                }
            },
        )
        execution.whatsapp_message = message
        execution.save(update_fields=["whatsapp_message", "updated_at"])

        from services.channels.hosted_whatsapp_transport import send_hosted_message

        send_hosted_message(message=message)
        finished = timezone.now()
        execution.status = FollowupExecution.Status.SENT
        execution.finished_at = finished
        execution.payload = {
            "provider": "hosted",
            "hosted_content_hash": content_hash,
            "message_id": str(message.id),
            "external_id": message.external_id or "",
        }
        execution.save(
            update_fields=["status", "finished_at", "payload", "updated_at"]
        )
        state.last_sent_at = finished
        state.paused_until = None
        state.save(update_fields=["last_sent_at", "paused_until", "updated_at"])
        _repeat_or_advance(state, step, completed_at=finished)
        return True
    except HostedAutomationPaused as exc:
        execution.status = FollowupExecution.Status.PENDING
        execution.started_at = None
        execution.scheduled_for = exc.paused_until
        execution.save(
            update_fields=["status", "started_at", "scheduled_for", "updated_at"]
        )
        _defer_state(state, exc.paused_until)
        return False
    except Exception as exc:
        _handle_failure(state, execution, exc)
        return False


def dispatch_one_hosted_due_state():
    if not cache.add(HOSTED_FOLLOWUP_LOCK, "1", timeout=18):
        return {"status": "locked"}
    try:
        now = timezone.now()
        state_id = (
            LeadSequenceState.objects.filter(
                status=LeadSequenceState.Status.ACTIVE,
                lead_auto_followup_enabled=True,
                sequence__is_active=True,
                sequence__whatsapp_account__connection_type=HOSTED_CONNECTION_TYPE,
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
        processed = process_hosted_due_state(state_id)
        return {
            "status": "processed" if processed else "deferred",
            "state_id": str(state_id),
        }
    finally:
        cache.delete(HOSTED_FOLLOWUP_LOCK)


def dispatch_one_api_due_state():
    """Keep the original Meta engine isolated from Hosted Account states."""
    if not cache.add(API_FOLLOWUP_LOCK, "1", timeout=18):
        return {"status": "locked"}
    try:
        now = timezone.now()
        state_id = (
            LeadSequenceState.objects.filter(
                status=LeadSequenceState.Status.ACTIVE,
                lead_auto_followup_enabled=True,
                sequence__is_active=True,
                sequence__whatsapp_account__connection_type="api",
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
        from services.followup_service import process_due_state

        processed = process_due_state(state_id)
        return {
            "status": "processed" if processed else "deferred",
            "state_id": str(state_id),
        }
    finally:
        cache.delete(API_FOLLOWUP_LOCK)


def hosted_queue_items(*, account):
    items = []
    jobs = (
        HostedAutomationJob.objects.filter(
            account=account,
            status__in=[HostedAutomationJob.Status.QUEUED, HostedAutomationJob.Status.PROCESSING],
        )
        .select_related("lead")
        .order_by("available_at")[:100]
    )
    for job in jobs:
        items.append(
            {
                "id": str(job.id),
                "to": job.lead.phone,
                "lead": job.lead.name,
                "body": "AI response to the latest lead message",
                "message_type": "AI Engagement",
                "created_at": job.created_at.isoformat(),
                "available_at": job.available_at.isoformat(),
                "origin": "AI Engagement",
                "priority": 1,
            }
        )

    states = (
        LeadSequenceState.objects.filter(
            sequence__whatsapp_account=account,
            status=LeadSequenceState.Status.ACTIVE,
            lead_auto_followup_enabled=True,
            next_step__isnull=False,
        )
        .select_related("lead", "next_step", "sequence")
        .order_by("upcoming_send_at", "assigned_at")[:100]
    )
    for state in states:
        step = state.next_step
        body = step.title or "Follow-up"
        if step.step_type == FollowupStep.StepType.WHATSAPP:
            try:
                body = step.hosted_config.body
            except HostedFollowupStepConfig.DoesNotExist:
                pass
        items.append(
            {
                "id": str(state.id),
                "to": state.lead.phone,
                "lead": state.lead.name,
                "body": body,
                "message_type": step.get_step_type_display(),
                "created_at": state.assigned_at.isoformat(),
                "available_at": state.upcoming_send_at.isoformat() if state.upcoming_send_at else "",
                "origin": "Auto Follow-up",
                "priority": 2,
            }
        )
    return sorted(items, key=lambda item: (item["priority"], item.get("available_at") or item["created_at"]))
