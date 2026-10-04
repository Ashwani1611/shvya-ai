"""One durable conversational AI send gate per WhatsApp sender.

Hosted conversational AI uses a fixed 45-second gap. Meta API conversational
AI uses a short configurable pacing gap after the inbound burst collector;
follow-up cadence timing still controls when bump-ups/follow-ups are created.
Redis/Celery determine when to wake work; PostgreSQL decides who may send.
Reservations commit before provider I/O and survive a worker crash.
"""

from datetime import timedelta
from functools import wraps
import os
import uuid

from django.db import transaction
from django.db.models import Case, DateTimeField, IntegerField, Max, Q, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.channels.models import AIMessageSendState, WhatsAppAccount, WhatsAppMessage
from services.channels.hosted_automation_service import HostedAutomationPaused


def _conversation_send_gap_seconds() -> int:
    try:
        value = int(os.getenv("AI_CONVERSATION_SEND_GAP_SECONDS", "5"))
    except (TypeError, ValueError):
        value = 5
    return min(max(value, 1), 45)


AI_SEND_GAP_SECONDS = _conversation_send_gap_seconds()


def ai_send_gap_seconds(account):
    return 45 if account.connection_type == "hosted" else AI_SEND_GAP_SECONDS

# Longer than the transport's longest bounded HTTP send (90 seconds).
AI_SEND_LEASE_SECONDS = 300
SENT_STATUSES = ("sent", "delivered", "read")


class AIMessageDeferred(HostedAutomationPaused):
    def __init__(self, available_at, reason="ai_send_gap"):
        self.available_at = available_at
        self.reason = reason
        super().__init__(available_at)


def is_ai_message(message):
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    return bool(payload.get("shvya_ai") or payload.get("shvya_welcome"))


def _last_recorded_ai_send(account):
    return WhatsAppMessage.objects.filter(
        account_id=account.pk, organization_id=account.organization_id,
        direction="outbound", status__in=SENT_STATUSES,
    ).filter(Q(raw_payload__has_key="shvya_ai") | Q(raw_payload__has_key="shvya_welcome")).aggregate(
        latest=Max(Coalesce("sent_at", "updated_at")),
    )["latest"]


def next_ai_send_at(account):
    state = AIMessageSendState.objects.filter(account_id=account.pk).first()
    if state is None:
        # First use after an upgrade must respect the previous release's most
        # recent successful send too. Legacy receipt time is conservative when
        # exact historical sent_at is unavailable.
        previous = _last_recorded_ai_send(account)
        return previous + timedelta(seconds=ai_send_gap_seconds(account)) if previous else None
    now = timezone.now()
    if state.claimed_until and state.claimed_until > now:
        # The provider may finish in a second. Recheck promptly instead of
        # persisting the conservative crash lease as every follower's ETA.
        return min(state.claimed_until, now + timedelta(seconds=5))
    return state.next_send_at


def _priority_wait(message, now):
    """All welcomes first, then FIFO replies, including legacy queued rows."""
    from apps.hosted_automation.models import HostedAutomationJob

    payload = message.raw_payload or {}
    welcome = bool(payload.get("shvya_welcome"))
    meta = payload.get("shvya_welcome") or payload.get("shvya_ai") or {}
    job_id = str(meta.get("job_id") or "")
    jobs = HostedAutomationJob.objects.filter(
        organization_id=message.organization_id, account_id=message.account_id,
        status__in=["queued", "processing"], available_at__lte=now,
    ).annotate(send_priority=Case(
        When(kind="welcome", then=Value(0)), default=Value(1), output_field=IntegerField(),
    )).order_by("send_priority", "created_at", "id")
    queued_at = parse_datetime(str(meta.get("queued_at") or "")) or message.created_at
    own_key = (0 if welcome else 1, queued_at, str(message.pk))
    if not welcome and message.account.connection_type == "api":
        # LLM calls can finish out of order. A later generated reply cannot
        # jump ahead of an earlier inbound turn still waiting for generation.
        source_id = meta.get("source_inbound_message_id")
        waiting = WhatsAppMessage.objects.filter(
            organization_id=message.organization_id, account_id=message.account_id,
            direction="inbound", created_at__lt=queued_at,
            raw_payload__shvya_ai_execution__status__in=["queued", "processing", "retrying"],
        ).filter(Q(raw_payload__shvya_ai_processing__processed__isnull=True)
                 | Q(raw_payload__shvya_ai_processing__processed=False))
        if source_id:
            waiting = waiting.exclude(pk=source_id)
        if waiting.exists():
            return now + timedelta(seconds=5)
    if job_id:
        own_job = HostedAutomationJob.objects.filter(
            pk=job_id, organization_id=message.organization_id, account_id=message.account_id,
        ).first()
        if own_job:
            own_key = (0 if own_job.kind == "welcome" else 1, own_job.created_at, str(own_job.pk))
    first = jobs.first()
    if first and str(first.pk) != job_id:
        key = (0 if first.kind == "welcome" else 1, first.created_at, str(first.pk))
        if key < own_key:
            return now + timedelta(seconds=5)

    # API replies/bump-ups from the canonical sender and pre-upgrade welcomes
    # may not have a durable job. Compare the SAME priority/arrival key across
    # both stores; separate precedence checks would deadlock an old message
    # against the job that is also waiting for it.
    pending = WhatsAppMessage.objects.filter(
        organization_id=message.organization_id, account_id=message.account_id,
        direction="outbound", status__in=["queued", "sending"],
    ).filter(Q(raw_payload__has_key="shvya_ai") | Q(raw_payload__has_key="shvya_welcome"))
    pending = pending.exclude(pk=message.pk).filter(
        raw_payload__shvya_ai__job_id__isnull=True,
        raw_payload__shvya_welcome__job_id__isnull=True,
    ).annotate(
        send_priority=Case(
            When(raw_payload__has_key="shvya_welcome", then=Value(0)),
            default=Value(1), output_field=IntegerField(),
        ),
        queue_arrival=Coalesce(
            Cast(KT("raw_payload__shvya_ai__queued_at"), DateTimeField()), "created_at",
        ),
    ).order_by("send_priority", "queue_arrival", "id")
    first_message = pending.first()
    if first_message:
        key = (first_message.send_priority, first_message.queue_arrival, str(first_message.pk))
        if key < own_key:
            return now + timedelta(seconds=5)
    return None


@transaction.atomic
def _reserve(message):
    now = timezone.now()
    # Lock an existing parent row to serialize first-use state creation. No
    # Redis lock is required and a busy number cannot block unrelated numbers.
    account = WhatsAppAccount.objects.select_for_update(skip_locked=True).filter(
        pk=message.account_id, organization_id=message.organization_id,
    ).first()
    if account is None:
        raise AIMessageDeferred(now + timedelta(seconds=5), "ai_sender_busy")
    message.refresh_from_db()
    if message.status in SENT_STATUSES:
        return None
    if message.status not in ("queued", "sending"):
        from services.channels.whatsapp_service import WhatsAppSendError
        raise WhatsAppSendError("AI message is no longer queued.")
    state, created = AIMessageSendState.objects.get_or_create(account=account)
    if created:
        previous = _last_recorded_ai_send(account)
        if previous:
            state.last_sent_at = previous
            state.next_send_at = previous + timedelta(seconds=ai_send_gap_seconds(account))
            state.save(update_fields=["last_sent_at", "next_send_at"])
    state = AIMessageSendState.objects.select_for_update().get(pk=state.pk)
    if state.claimed_until and state.claimed_until > now:
        raise AIMessageDeferred(min(state.claimed_until, now + timedelta(seconds=5)), "ai_sender_busy")
    available = max([t for t in (state.next_send_at, now) if t])
    if available > now:
        raise AIMessageDeferred(available)
    priority_wait = _priority_wait(message, now)
    if priority_wait:
        raise AIMessageDeferred(priority_wait, "ai_queue_priority")
    token = uuid.uuid4()
    state.claim_token = token
    state.claimed_until = now + timedelta(seconds=AI_SEND_LEASE_SECONDS)
    state.next_send_at = state.claimed_until + timedelta(seconds=ai_send_gap_seconds(account))
    state.save(update_fields=["claim_token", "claimed_until", "next_send_at"])
    return token


def _finish(message, token, *, deferred=False):
    now = timezone.now()
    updates = {
        "claim_token": None, "claimed_until": None,
        "next_send_at": now + timedelta(seconds=ai_send_gap_seconds(message.account)),
    }
    if deferred:
        # No provider call occurred. Release capacity without charging a new
        # pacing gap on each fairness/priority retry.
        state = AIMessageSendState.objects.filter(account_id=message.account_id, claim_token=token).first()
        if state is None:
            return
        updates["next_send_at"] = (
            state.last_sent_at + timedelta(seconds=ai_send_gap_seconds(message.account))
            if state.last_sent_at else None
        )
    if message.status in SENT_STATUSES:
        sent_at = message.sent_at or now
        updates["last_sent_at"] = sent_at
        updates["next_send_at"] = sent_at + timedelta(seconds=ai_send_gap_seconds(message.account))
    AIMessageSendState.objects.filter(account_id=message.account_id, claim_token=token).update(**updates)


def _admit_ai_provider(message):
    """Charge provider capacity only after this message can actually send."""
    from django.conf import settings
    from apps.core.fairness import admit_provider_start

    hosted = message.account.connection_type == "hosted"
    provider = "hosted_whatsapp" if hosted else "whatsapp"
    allowed, retry_after, scope = admit_provider_start(
        provider=provider, account_id=message.account_id,
        account_limit=(settings.HOSTED_WHATSAPP_ACCOUNT_SENDS_PER_MINUTE
                       if hosted else settings.WHATSAPP_ACCOUNT_SENDS_PER_MINUTE),
        global_limit=(settings.HOSTED_WHATSAPP_GLOBAL_SENDS_PER_MINUTE
                      if hosted else settings.WHATSAPP_GLOBAL_SENDS_PER_MINUTE),
    )
    if not allowed:
        raise AIMessageDeferred(
            timezone.now() + timedelta(seconds=max(1, int(retry_after or 1))),
            f"{provider}_{scope}_fairness_limit",
        )


def paced_ai_send(sender):
    """Wrap an actual provider send, including template welcomes and bump-ups."""
    @wraps(sender)
    def send(*args, **kwargs):
        message = kwargs.get("message") or (args[0] if args else None)
        if message is None:
            return sender(*args, **kwargs)
        # Metadata can be stamped after the caller first loaded this row.
        # Classification itself must use the persisted message, not that stale
        # caller object, otherwise it could bypass every AI control below.
        message.refresh_from_db()
        if not is_ai_message(message):
            return sender(*args, **kwargs)
        from services.channels.hosted_whatsapp_service import account_ai_block_reason
        from services.channels.whatsapp_service import WhatsAppSendError

        if message.account.organization_id != message.organization_id:
            raise WhatsAppSendError("WhatsApp account does not belong to the message organization.")
        # Stale Celery objects cannot resend an already delivered row or bypass
        # a lead/stage/account toggle changed after the queue was created.
        message.refresh_from_db()
        if message.status in SENT_STATUSES:
            return message
        if message.status not in ("queued", "sending"):
            raise WhatsAppSendError("AI message is no longer queued.")
        ai = (message.raw_payload or {}).get("shvya_ai") or {}
        reason = account_ai_block_reason(
            account=message.account, lead=message.lead,
            bump_up_number=(ai.get("number", 1) if ai.get("origin") == "bump_up" else None),
        )
        if reason:
            message.status = WhatsAppMessage.Status.FAILED
            message.error = f"AI send cancelled: {reason}"
            message.save(update_fields=["status", "error", "updated_at"])
            raise WhatsAppSendError(message.error)
        token = _reserve(message)
        if token is None:
            return message
        deferred = False
        try:
            _admit_ai_provider(message)
            return sender(*args, **kwargs)
        except AIMessageDeferred:
            deferred = True
            raise
        finally:
            if deferred:
                _finish(message, token, deferred=True)
            else:
                _finish(message, token)
    return send
