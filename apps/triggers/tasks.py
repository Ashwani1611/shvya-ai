import logging

from celery import shared_task
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def dispatch_smart_triggers():
    # PostgreSQL session lock prevents overlapping Beat deliveries from
    # executing ordered actions concurrently. It is released on disconnect.
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

    scan_timers()
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
        deliver_email(run_id)
    from apps.channels.tasks import send_whatsapp_message_task

    for run in TriggerRun.objects.filter(status="queued").select_related("message")[
        :100
    ]:
        if run.message and run.message.status == "queued":
            send_whatsapp_message_task.delay(str(run.message_id))
        elif run.message:
            run.status = (
                "completed"
                if run.message.status in ("sent", "delivered", "read")
                else "failed"
            )
            run.detail = run.message.error
            run.save(update_fields=["status", "detail"])
