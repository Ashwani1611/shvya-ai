import logging
from datetime import timedelta

from celery import shared_task
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def dispatch_smart_triggers():
    # PostgreSQL session lock serializes rule order across Beat deliveries.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [831947201])
        if not cursor.fetchone()[0]:
            return
    try:
        _dispatch()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [831947201])


def _dispatch():
    from apps.triggers.models import TriggerEvent, TriggerRun
    from services.triggers.actions import deliver_email, execute
    from services.triggers.evaluator import evaluate, scan_timers

    # A bad legacy timer must not stall unrelated events or already-due actions.
    try:
        scan_timers()
    except Exception:
        logger.exception("Smart Trigger timer scan failed")
    for event_id in (
        TriggerEvent.objects.filter(processed_at__isnull=True)
        .order_by("created_at")
        .values_list("id", flat=True)[:500]
    ):
        try:
            evaluate(event_id)
        except Exception:
            logger.exception("Smart Trigger event failed: %s", event_id)
    for run_id in (
        TriggerRun.objects.filter(
            status__in=["pending", "scheduled"], due_at__lte=timezone.now()
        )
        .order_by("event__created_at", "rule__position", "rule__created_at")
        .values_list("id", flat=True)[:500]
    ):
        try:
            execute(run_id)
        except Exception:
            logger.exception("Smart Trigger run failed: %s", run_id)
    for run_id in TriggerRun.objects.filter(status="email_ready").values_list(
        "id", flat=True
    )[:100]:
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
    runs = TriggerRun.objects.filter(
        status__in=["queued", "dispatching"]
    ).select_related("message").order_by("due_at", "created_at")[:500]
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
