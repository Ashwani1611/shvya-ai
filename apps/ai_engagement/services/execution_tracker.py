"""Durable, content-free AI turn progress and bounded broker recovery."""
from datetime import timedelta
import logging
import re

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_datetime

KEY = 'shvya_ai_execution'
logger = logging.getLogger(__name__)


def record_execution(message_id, *, status, reason='', increment=False):
    from apps.channels.models import WhatsAppMessage
    reason = reason if re.fullmatch(r'[a-z][a-z0-9_]{0,79}', reason or '') else ''
    with transaction.atomic():
        message = WhatsAppMessage.objects.select_for_update().filter(pk=message_id, direction='inbound').first()
        if message is None:
            return
        payload = dict(message.raw_payload or {})
        previous = payload.get(KEY) or {}
        # Duplicate tasks must not replace the successful turn's final state.
        if (payload.get('shvya_ai_processing') or {}).get('processed') and status in {'processing', 'skipped', 'queued'}:
            return
        payload[KEY] = {**previous, 'status': status, 'reason': reason,
            'attempts': int(previous.get('attempts', 0)) + int(increment),
            'updated_at': timezone.now().isoformat()}
        WhatsAppMessage.objects.filter(pk=message.pk).update(raw_payload=payload)


def record_instagram_execution(message_id, *, status, reason="", increment=False):
    """Persist durable Instagram AI-turn progress on the inbound provider row."""
    from apps.channels.instagram_models import InstagramMessage

    reason = (
        reason
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason or "")
        else ""
    )
    with transaction.atomic():
        message = (
            InstagramMessage.objects.select_for_update()
            .filter(
                pk=message_id,
                direction=InstagramMessage.Direction.INBOUND,
            )
            .first()
        )
        if message is None:
            return

        payload = dict(message.raw_payload or {})
        previous = payload.get(KEY) or {}
        if (
            (payload.get("shvya_ai_processing") or {}).get("processed")
            and status in {"processing", "skipped", "queued"}
        ):
            return

        payload[KEY] = {
            **previous,
            "status": status,
            "reason": reason,
            "attempts": int(previous.get("attempts", 0)) + int(increment),
            "updated_at": timezone.now().isoformat(),
        }
        InstagramMessage.objects.filter(pk=message.pk).update(raw_payload=payload)


def publish_instagram_engagement(message_id):
    """Publish one durable Instagram AI turn after its DB transaction commits."""
    from apps.channels.instagram_tasks import generate_instagram_ai_engagement_task

    try:
        return generate_instagram_ai_engagement_task.apply_async(
            args=[str(message_id)],
            countdown=0,
        )
    except Exception:
        logger.exception(
            "Instagram AI task publication failed; durable turn retained for recovery"
        )
        return None


def claim_execution(message_id, *, stale_after_seconds=180):
    """Atomically claim one inbound AI turn before generation.

    This durable claim complements the Redis generation lock. Duplicate Celery
    deliveries for the same inbound message are rejected while a fresh worker is
    processing it, and stale claims remain recoverable by the existing recovery
    task.
    """
    from apps.channels.models import WhatsAppMessage

    now = timezone.now()
    with transaction.atomic():
        message = (
            WhatsAppMessage.objects.select_for_update()
            .filter(pk=message_id, direction='inbound')
            .first()
        )
        if message is None:
            return False

        payload = dict(message.raw_payload or {})
        if (payload.get('shvya_ai_processing') or {}).get('processed'):
            return False

        previous = payload.get(KEY) or {}
        if previous.get('status') == 'processing':
            stamp = parse_datetime(str(previous.get('updated_at') or ''))
            if stamp and (now - stamp).total_seconds() < stale_after_seconds:
                return False

        payload[KEY] = {
            **previous,
            'status': 'processing',
            'reason': '',
            'attempts': int(previous.get('attempts', 0)) + 1,
            'updated_at': now.isoformat(),
        }
        WhatsAppMessage.objects.filter(pk=message.pk).update(raw_payload=payload)
        return True


def queue_api_engagement(*, lead_id, source_message_id=None):
    from apps.channels.models import WhatsAppMessage
    from apps.ai_engagement.tasks import generate_ai_engagement_response

    messages = WhatsAppMessage.objects.filter(
        lead_id=lead_id,
        direction='inbound',
        account__connection_type='api',
    )
    if source_message_id is not None:
        messages = messages.filter(pk=source_message_id)
    message = messages.order_by('-created_at', '-id').first()
    if message is None:
        return None

    record_execution(message.pk, status='queued')
    # If publication fails, the durable queued marker survives for Beat recovery.
    try:
        return generate_ai_engagement_response.apply_async(args=[str(lead_id)], countdown=0)
    except Exception:
        logger.exception('AI task publication failed; durable turn retained for recovery')
        return None



def defer_ai_delivery(*, message_id, available_at, reason):
    """Keep pacing waits durable without consuming the provider retry budget."""
    from apps.channels.models import WhatsAppMessage

    with transaction.atomic():
        message = WhatsAppMessage.objects.select_for_update().filter(pk=message_id).first()
        if message is None or message.status in {"sent", "delivered", "read"}:
            return
        payload = dict(message.raw_payload or {})
        delivery = dict(payload.get("shvya_ai_delivery") or {})
        payload["shvya_ai_delivery"] = {
            **delivery, "available_at": available_at.isoformat(), "reason": reason,
        }
        message.raw_payload = payload
        message.status = "queued"
        message.error = ""
        message.save(update_fields=["raw_payload", "status", "error", "updated_at"])


def _due_json_time(path, cutoff):
    # Missing timestamps are legacy durable work and must remain recoverable.
    return Q(**{f"{path}__isnull": True}) | Q(**{f"{path}__lte": cutoff.isoformat()})


def _release_abandoned_delivery_heads(now):
    """A lost worker must not block its sender's entire FIFO indefinitely.

    The provider might already have accepted this message. Mark an expired
    standalone send as uncertain for review; never replay its request. Ten
    minutes exceeds both the 90-second transport timeout and five-minute lease.
    """
    from apps.channels.models import WhatsAppMessage

    cutoff = now - timedelta(minutes=10)
    abandoned = WhatsAppMessage.objects.filter(
        direction="outbound", status="sending", updated_at__lte=cutoff,
        raw_payload__shvya_ai__job_id__isnull=True,
        raw_payload__shvya_welcome__job_id__isnull=True,
    ).filter(
        Q(raw_payload__has_key="shvya_ai") | Q(raw_payload__has_key="shvya_welcome")
    ).order_by("updated_at", "id").values_list("pk", flat=True)[:100]
    for message_id in abandoned:
        with transaction.atomic():
            message = WhatsAppMessage.objects.select_for_update(skip_locked=True).filter(
                pk=message_id, status="sending", updated_at__lte=cutoff,
            ).first()
            if message is None:
                continue
            payload = dict(message.raw_payload or {})
            payload["shvya_ai_delivery"] = {
                **(payload.get("shvya_ai_delivery") or {}),
                "reason": "provider_outcome_uncertain",
            }
            WhatsAppMessage.objects.filter(pk=message.pk).update(
                status="failed", raw_payload=payload, updated_at=now,
                error="Provider outcome unknown after worker interruption. Check delivery before retrying.",
            )


def recover_api_engagement():
    """Recover eligible durable work, irrespective of backlog age.

    Filter cooldowns and ownership in SQL *before* the bounded batch. Otherwise
    the first hundred future/recent rows permanently hide older eligible work.
    Never replay untracked inbound chats or ambiguous in-flight provider sends.
    """
    from apps.channels.models import WhatsAppMessage
    from apps.ai_engagement.tasks import generate_ai_engagement_response
    from apps.channels.tasks import send_whatsapp_message_task

    now = timezone.now()
    _release_abandoned_delivery_heads(now)
    rescued = 0
    execution_time = "raw_payload__shvya_ai_execution__updated_at"
    pending = WhatsAppMessage.objects.filter(
        direction="inbound", account__connection_type="api", lead__isnull=False,
    ).filter(
        (Q(raw_payload__shvya_ai_execution__status="processing")
         & _due_json_time(execution_time, now - timedelta(seconds=180)))
        | (Q(raw_payload__shvya_ai_execution__status__in=["queued", "retrying"])
           & _due_json_time(execution_time, now - timedelta(seconds=30)))
    ).filter(
        Q(raw_payload__shvya_ai_processing__processed__isnull=True)
        | ~Q(raw_payload__shvya_ai_processing__processed=True)
    ).select_related("lead").order_by("created_at", "id")[:100]
    for message in pending:
        with transaction.atomic():
            locked = WhatsAppMessage.objects.select_for_update(skip_locked=True).filter(pk=message.pk).first()
            if locked is None:
                continue
            payload = locked.raw_payload or {}
            execution = payload.get(KEY) or {}
            status = execution.get("status")
            if status not in {"queued", "processing", "retrying"} or (payload.get("shvya_ai_processing") or {}).get("processed"):
                continue
            stamp = parse_datetime(str(execution.get("updated_at") or ""))
            grace = 180 if status == "processing" else 30
            if stamp and (now - stamp).total_seconds() < grace:
                # A worker/recovery process renewed this claim after our scan.
                continue
            if int(execution.get("attempts", 0)) >= 5:
                record_execution(message.pk, status="failed", reason="retry_limit_reached")
                continue
            latest = message.lead.whatsapp_messages.filter(direction="inbound").order_by("-created_at", "-id").first()
            if latest is None or latest.pk != message.pk:
                record_execution(message.pk, status="skipped", reason="conversation_changed")
                continue
            record_execution(message.pk, status="queued", reason="recovered_after_dispatch_delay")
        try:
            generate_ai_engagement_response.apply_async(args=[str(message.lead_id)], countdown=0)
            rescued += 1
        except Exception:
            logger.exception("AI recovery could not publish a retained turn")

    # Instagram uses the same durable execution marker. Recover only the
    # latest inbound for each exact Instagram conversation; stale earlier DMs are
    # marked skipped so a burst is answered once using the newest context.
    from apps.channels.instagram_models import InstagramMessage
    from apps.channels.instagram_tasks import (
        generate_instagram_ai_engagement_task,
        send_instagram_message_task,
    )

    instagram_pending = (
        InstagramMessage.objects.filter(
            direction=InstagramMessage.Direction.INBOUND,
            conversation__lead__isnull=False,
        )
        .filter(
            (
                Q(raw_payload__shvya_ai_execution__status="processing")
                & _due_json_time(
                    execution_time,
                    now - timedelta(seconds=180),
                )
            )
            | (
                Q(
                    raw_payload__shvya_ai_execution__status__in=[
                        "queued",
                        "retrying",
                    ]
                )
                & _due_json_time(
                    execution_time,
                    now - timedelta(seconds=30),
                )
            )
        )
        .filter(
            Q(raw_payload__shvya_ai_processing__processed__isnull=True)
            | ~Q(raw_payload__shvya_ai_processing__processed=True)
        )
        .select_related("conversation", "conversation__lead")
        .order_by("created_at", "id")[:100]
    )
    for message in instagram_pending:
        with transaction.atomic():
            locked = (
                InstagramMessage.objects.select_for_update(skip_locked=True)
                .select_related("conversation", "conversation__lead")
                .filter(pk=message.pk)
                .first()
            )
            if locked is None:
                continue

            payload = locked.raw_payload or {}
            execution = payload.get(KEY) or {}
            status = execution.get("status")
            if (
                status not in {"queued", "processing", "retrying"}
                or (payload.get("shvya_ai_processing") or {}).get("processed")
            ):
                continue

            stamp = parse_datetime(str(execution.get("updated_at") or ""))
            grace = 180 if status == "processing" else 30
            if stamp and (now - stamp).total_seconds() < grace:
                continue
            if int(execution.get("attempts", 0)) >= 5:
                record_instagram_execution(
                    locked.pk,
                    status="failed",
                    reason="retry_limit_reached",
                )
                continue

            latest = (
                locked.conversation.messages.filter(
                    direction=InstagramMessage.Direction.INBOUND,
                )
                .order_by("-created_at", "-id")
                .first()
            )
            if latest is None or latest.pk != locked.pk:
                record_instagram_execution(
                    locked.pk,
                    status="skipped",
                    reason="conversation_changed",
                )
                continue

            record_instagram_execution(
                locked.pk,
                status="queued",
                reason="recovered_after_dispatch_delay",
            )

        try:
            generate_instagram_ai_engagement_task.apply_async(
                args=[str(message.pk)],
                countdown=0,
            )
            rescued += 1
        except Exception:
            logger.exception(
                "Instagram AI recovery could not publish a retained turn"
            )

    # Generated replies, welcome messages and bump-ups share the same durable
    # delivery recovery. A welcome/bump-up need not have a source inbound turn.
    outgoing = WhatsAppMessage.objects.filter(
        direction="outbound", status="queued", created_at__lte=now - timedelta(seconds=10),
    ).filter(
        Q(raw_payload__has_key="shvya_ai") | Q(raw_payload__has_key="shvya_welcome")
    ).filter(
        Q(account__connection_type="api") | Q(raw_payload__shvya_ai__origin="bump_up")
        | Q(raw_payload__has_key="shvya_welcome")
    ).filter(
        raw_payload__shvya_ai__job_id__isnull=True,
        raw_payload__shvya_welcome__job_id__isnull=True,
    ).filter(
        _due_json_time("raw_payload__shvya_ai_delivery__available_at", now),
        _due_json_time("raw_payload__shvya_ai_delivery__recovery_dispatched_at", now - timedelta(seconds=15)),
    ).annotate(send_priority=Case(
        When(raw_payload__has_key="shvya_welcome", then=Value(0)),
        default=Value(1), output_field=IntegerField(),
    )).order_by("send_priority", "created_at", "id")[:100]
    for message in outgoing:
        with transaction.atomic():
            locked = WhatsAppMessage.objects.select_for_update(skip_locked=True).filter(
                pk=message.pk, status="queued",
            ).first()
            if locked is None:
                continue
            payload = dict(locked.raw_payload or {})
            delivery = dict(payload.get("shvya_ai_delivery") or {})
            available_at = parse_datetime(str(delivery.get("available_at") or ""))
            if available_at and available_at > now:
                continue
            stamp = parse_datetime(str(delivery.get("recovery_dispatched_at") or ""))
            if stamp and (now - stamp).total_seconds() < 15:
                continue
            delivery["recovery_dispatched_at"] = now.isoformat()
            payload["shvya_ai_delivery"] = delivery
            WhatsAppMessage.objects.filter(pk=locked.pk).update(raw_payload=payload)
        try:
            send_whatsapp_message_task.delay(str(message.pk))
            rescued += 1
        except Exception:
            logger.exception("AI recovery could not publish a saved reply")

    instagram_outgoing = (
        InstagramMessage.objects.filter(
            direction=InstagramMessage.Direction.OUTBOUND,
            status=InstagramMessage.Status.QUEUED,
            created_at__lte=now - timedelta(seconds=10),
            raw_payload__has_key="shvya_ai",
        )
        .order_by("created_at", "id")[:100]
    )
    for message in instagram_outgoing:
        try:
            send_instagram_message_task.delay(str(message.pk))
            rescued += 1
        except Exception:
            logger.exception(
                "Instagram AI recovery could not publish a saved reply"
            )

    return {"requeued": rescued}
