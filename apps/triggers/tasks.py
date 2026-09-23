import logging
from datetime import timedelta
import uuid

from celery import shared_task
from django.conf import settings
from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.utils import timezone
import redis

logger = logging.getLogger(__name__)


@shared_task
def dispatch_smart_triggers():
    # A PostgreSQL session advisory lock is unsafe behind transaction-pooled
    # PgBouncer because lock/unlock can use different server connections.
    # Redis provides a short, token-fenced dispatcher lease across workers.
    client = redis.Redis.from_url(settings.CACHE_REDIS_URL)
    key = "shvya:automation:trigger-dispatch"
    owner = str(uuid.uuid4())
    try:
        if not client.set(key, owner, nx=True, ex=600):
            return
    except Exception:
        logger.exception("Smart Trigger dispatch lease is unavailable")
        return
    try:
        _dispatch()
    finally:
        try:
            client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                key,
                owner,
            )
        except Exception:
            logger.exception("Smart Trigger dispatch lease release failed")


def _fair_queryset(
    queryset,
    *,
    partition_by,
    order_by,
    per_organization,
    limit,
):
    """Bound one dispatch pass while reserving capacity for each tenant."""
    ranked = queryset.annotate(
        _tenant_rank=Window(
            expression=RowNumber(),
            partition_by=[F(partition_by)],
            order_by=[F(order_by).asc(), F("id").asc()],
        )
    ).filter(_tenant_rank__lte=max(1, int(per_organization)))
    return ranked.order_by(order_by, "id")[: max(1, int(limit))]


def _dispatch():
    from apps.triggers.models import TriggerEvent, TriggerRun
    from services.triggers.actions import deliver_email, execute
    from services.triggers.evaluator import evaluate, scan_timers

    # A bad legacy timer must not stall unrelated events or already-due actions.
    try:
        scan_timers()
    except Exception:
        logger.exception("Smart Trigger timer scan failed")
    pending_events = TriggerEvent.objects.filter(processed_at__isnull=True)
    for event_id in _fair_queryset(
        pending_events,
        partition_by="organization_id",
        order_by="created_at",
        per_organization=10,
        limit=500,
    ).values_list("id", flat=True):
        try:
            evaluate(event_id)
        except Exception:
            logger.exception("Smart Trigger event failed: %s", event_id)
    due_runs = TriggerRun.objects.filter(
        status__in=["pending", "scheduled"],
        due_at__lte=timezone.now(),
    )
    for run_id in _fair_queryset(
        due_runs,
        partition_by="rule__organization_id",
        order_by="due_at",
        per_organization=10,
        limit=500,
    ).values_list("id", flat=True):
        try:
            execute(run_id)
        except Exception:
            logger.exception("Smart Trigger run failed: %s", run_id)
    email_runs = TriggerRun.objects.filter(status="email_ready")
    for run_id in _fair_queryset(
        email_runs,
        partition_by="rule__organization_id",
        order_by="due_at",
        per_organization=5,
        limit=100,
    ).values_list("id", flat=True):
        try:
            deliver_email(run_id)
        except Exception:
            logger.exception("Smart Trigger email dispatch failed: %s", run_id)
    TriggerRun.objects.filter(status="sending", due_at__lte=timezone.now()).update(
        status="needs_review", finished_at=timezone.now(),
        detail="Email delivery was interrupted. Check the connected mailbox before retrying.",
    )
    _dispatch_messages()


def _dispatch_messages():
    """Reconcile transport state without resetting the sender's retry backoff.

    A durable dispatch lease prevents Beat from publishing a fresh task every
    ten seconds while the canonical sender is retrying. Only a still-queued,
    unchanged message can be recovered after ten minutes. An uncertain in-flight
    send is never retried automatically.
    """
    from apps.channels.tasks import send_whatsapp_message_task
    from apps.triggers.models import TriggerRun

    now = timezone.now()
    lease = timedelta(minutes=10)
    runs = _fair_queryset(
        TriggerRun.objects.filter(
            status__in=["queued", "dispatching"]
        ).select_related("message"),
        partition_by="rule__organization_id",
        order_by="due_at",
        per_organization=10,
        limit=500,
    )
    for run in runs:
        try:
            message = run.message
            if message is None:
                run.status, run.detail = "failed", "The queued WhatsApp message is unavailable."
            elif message.status in ("sent", "delivered", "read"):
                run.status, run.detail = "completed", "WhatsApp message sent."
            elif message.status == "failed":
                run.status, run.detail = "failed", message.error or "WhatsApp delivery failed."
            elif message.status == "sending":
                if message.updated_at > now - lease:
                    continue
                run.status, run.detail = (
                    "needs_review",
                    "WhatsApp delivery is uncertain. Check provider logs before retrying.",
                )
            elif message.status == "queued":
                if run.due_at > now:
                    continue
                if run.status == "dispatching" and message.updated_at > now - lease:
                    continue
                # Claim before publishing. A lost broker acknowledgement is
                # recovered with the same message, never a second message row.
                claimed = TriggerRun.objects.filter(
                    pk=run.pk, status=run.status, due_at=run.due_at
                ).update(status="dispatching", due_at=now + lease, finished_at=None)
                if not claimed:
                    continue
                try:
                    # Add a marker for messages queued before this release too.
                    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
                    if "shvya_workflow" not in payload:
                        message.raw_payload = {**payload, "shvya_workflow": {"run_id": str(run.id)}}
                        message.save(update_fields=["raw_payload", "updated_at"])
                    send_whatsapp_message_task.delay(str(message.id))
                except Exception:
                    TriggerRun.objects.filter(pk=run.pk, status="dispatching").update(
                        status="queued", due_at=now + timedelta(seconds=30),
                        detail="Delivery queue unavailable; retrying safely.",
                    )
                    logger.exception("Workflow message publication failed: %s", run.id)
                continue
            else:
                # Unknown/transient transport states must not appear as failure.
                continue
            run.finished_at = now
            run.save(update_fields=["status", "detail", "finished_at"])
        except Exception:
            logger.exception("Workflow message reconciliation failed: %s", run.id)
