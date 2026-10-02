"""Durable, account-serialized welcome/reply execution.

Database leases own work. Celery messages only wake it, so a broker restart,
redelivery, rate-limit, or provider retry cannot leave a job processing forever.
"""
import logging
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.hosted_automation.models import HostedAutomationJob
from services.channels.hosted_automation_service import (
    HOSTED_AI_PROCESSING_STALE_SECONDS,
    HostedAutomationPaused,
    hosted_ai_block_reason,
    hosted_job_allows_history,
    ordered_ai_jobs,
)
from services.channels.hosted_health_guard import hosted_health_pause_until

logger = logging.getLogger(__name__)
SENT_STATUSES = {WhatsAppMessage.Status.SENT, WhatsAppMessage.Status.DELIVERED, WhatsAppMessage.Status.READ}


class _DurableRetry(Exception):
    def __init__(self, exc=None, countdown=30):
        self.cause = exc
        self.countdown = countdown or 30
        super().__init__(str(exc or "AI generation retry"))


class _DurableTask:
    """Translate provider retry requests into durable queue state, not ETA tasks."""
    def __init__(self, job):
        self.request = SimpleNamespace(retries=int((job.result or {}).get("retry_count", 0)))

    def retry(self, exc=None, countdown=30, **kwargs):
        raise _DurableRetry(exc=exc, countdown=countdown)


def _owned(job):
    return HostedAutomationJob.objects.filter(
        pk=job.pk, status=HostedAutomationJob.Status.PROCESSING, claim_token=job.claim_token,
    )


def _finish(job, status, result, error=""):
    changed = _owned(job).update(
        status=status, completed_at=timezone.now(), result=result, error=str(error)[:2000],
        lease_expires_at=None, claim_token="", updated_at=timezone.now(),
    )
    if changed and status in {HostedAutomationJob.Status.FAILED, HostedAutomationJob.Status.SKIPPED}:
        message_id = (result or {}).get("message_id")
        if message_id:
            WhatsAppMessage.objects.filter(pk=message_id, status=WhatsAppMessage.Status.QUEUED).update(
                status=WhatsAppMessage.Status.FAILED,
                error=str(error or result.get("reason") or "AI job did not complete delivery")[:2000],
            )
    return result if changed else {"status": "skipped", "reason": "job_lease_replaced"}


def _defer(job, available_at, reason, error="", retry=False):
    result = {**(job.result or {}), "defer_reason": reason, "available_at": available_at.isoformat()}
    result.pop("_processing_task_id", None)
    if retry:
        result["retry_count"] = int(result.get("retry_count", 0)) + 1
        if result["retry_count"] > 8:
            return _finish(job, HostedAutomationJob.Status.FAILED,
                           {**result, "status": "failed", "reason": "retry_limit_exceeded"}, error)
    _owned(job).update(
        status=HostedAutomationJob.Status.QUEUED, available_at=available_at,
        started_at=None, completed_at=None, lease_expires_at=None, claim_token="",
        result=result, error=str(error)[:2000], updated_at=timezone.now(),
    )
    # Beat also wakes this database row if the broker is unavailable now.
    from apps.hosted_automation.signals import schedule_hosted_ai_wakeup
    schedule_hosted_ai_wakeup(available_at, job.account_id)
    return {"status": "deferred", "reason": reason, "available_at": available_at.isoformat()}


def _requeue_for_health(job, paused_until):
    return _defer(job, paused_until, "account_health_pause")


def _cancel_generated_message(job):
    from services.channels.hosted_automation_service import cancel_job_outbound
    cancel_job_outbound(job=job, reason="source_or_controls_changed")


def _send_generated_ai_message(job):
    message_id = (job.result or {}).get("message_id")
    message = WhatsAppMessage.objects.select_related("account", "lead").filter(
        id=message_id, organization=job.organization, account=job.account,
    ).first() if message_id else None
    if message is None:
        return {"status": "failed", "reason": "generated_message_missing"}
    if message.status in SENT_STATUSES:
        return {"status": "sent", "message_id": str(message.pk)}
    if message.status != WhatsAppMessage.Status.QUEUED:
        return {"status": "failed", "reason": "generated_message_not_queued", "message_id": str(message.pk)}

    # Upgrade pre-change/orphaned rows with their durable owner before the send
    # gate compares queue priority. A message must never block its own job.
    payload = dict(message.raw_payload or {})
    metadata_key = "shvya_welcome" if job.kind == HostedAutomationJob.Kind.WELCOME else "shvya_ai"
    payload[metadata_key] = {**(payload.get(metadata_key) or {}), "job_id": str(job.pk)}
    message.raw_payload = payload
    message.save(update_fields=["raw_payload", "updated_at"])

    from services.channels.whatsapp_service import WhatsAppSendError, send_outbound_message
    try:
        if job.account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
            from services.channels.hosted_whatsapp_transport import send_hosted_message
            send_hosted_message(message=message, defer_on_pause=True)
        else:
            send_outbound_message(message=message)
    except WhatsAppSendError as exc:
        cause = exc.__cause__
        status_code = getattr(cause, "status_code", None)
        if cause is not None and status_code is not None and (status_code == 429 or status_code >= 500):
            WhatsAppMessage.objects.filter(pk=message.pk).exclude(status__in=SENT_STATUSES).update(
                status=WhatsAppMessage.Status.QUEUED,
            )
            raise _DurableRetry(exc=exc, countdown=30) from exc
        return {"status": "failed", "reason": str(exc), "message_id": str(message.pk)}
    return {"status": "sent", "message_id": str(message.pk)}


def _claim(job_id):
    from services.channels.ai_send_gate import next_ai_send_at
    row = HostedAutomationJob.objects.filter(pk=job_id).values("account_id").first()
    if row is None:
        return None, {"status": "skipped", "reason": "job_not_found"}
    with transaction.atomic():
        account = WhatsAppAccount.objects.select_for_update(skip_locked=True).filter(pk=row["account_id"]).first()
        if account is None:
            return None, {"status": "deferred", "reason": "account_busy"}
        job = HostedAutomationJob.objects.select_for_update(of=("self",)).select_related(
            "account", "organization", "lead", "source_message",
        ).filter(pk=job_id).first()
        if job is None or job.status != HostedAutomationJob.Status.QUEUED:
            return None, {"status": "skipped", "reason": "job_already_claimed_or_finished"}
        pending = HostedAutomationJob.objects.filter(account=account, status__in=["queued", "processing"])
        if pending.filter(status="processing").exists():
            HostedAutomationJob.objects.filter(pk=job.pk).update(lease_expires_at=None)
            return None, {"status": "deferred", "reason": "account_processing"}
        first = ordered_ai_jobs(pending).first()
        if first is not None and first.pk != job.pk:
            HostedAutomationJob.objects.filter(pk=job.pk).update(lease_expires_at=None)
            return None, {"status": "deferred", "reason": "ai_queue_priority"}
        now = timezone.now()
        send_at = next_ai_send_at(account=account)
        ready_at = max(value for value in [job.available_at, send_at] if value is not None)
        if ready_at > now:
            HostedAutomationJob.objects.filter(pk=job.pk).update(available_at=ready_at, lease_expires_at=None)
            return None, {"status": "deferred", "reason": "not_due", "available_at": ready_at.isoformat()}
        job.status = HostedAutomationJob.Status.PROCESSING
        job.started_at = now
        job.lease_expires_at = now + timedelta(seconds=HOSTED_AI_PROCESSING_STALE_SECONDS)
        job.claim_token = uuid4().hex
        job.attempts += 1
        job.save(update_fields=["status", "started_at", "lease_expires_at", "claim_token", "attempts", "updated_at"])
        return job, None


def _recover_message(job):
    if (job.result or {}).get("message_id"):
        return
    messages = WhatsAppMessage.objects.filter(
        organization=job.organization, account=job.account, lead=job.lead,
        direction=WhatsAppMessage.Direction.OUTBOUND,
    )
    if job.kind == HostedAutomationJob.Kind.WELCOME:
        messages = messages.filter(raw_payload__shvya_welcome__job_id=str(job.pk))
    else:
        messages = messages.filter(raw_payload__shvya_ai__source_inbound_message_id=str(job.source_message_id))
    recovered = messages.order_by("-created_at", "-id").first()
    if recovered is not None:
        job.result = {**(job.result or {}), "status": "completed", "engaged": True,
                      "message_id": str(recovered.pk), "recovered_generated_message": True}
        _owned(job).update(result=job.result)


def _execute(job):
    _recover_message(job)
    message_id = (job.result or {}).get("message_id")
    if message_id and WhatsAppMessage.objects.filter(pk=message_id, status__in=SENT_STATUSES).exists():
        return _finish(job, HostedAutomationJob.Status.COMPLETED,
                       {**job.result, "delivery": {"status": "sent", "message_id": message_id}})

    if job.kind != HostedAutomationJob.Kind.WELCOME:
        if job.source_message is None:
            reason = "source_message_missing"
        else:
            source_payload = job.source_message.raw_payload or {}
            latest = job.lead.whatsapp_messages.filter(
                organization=job.organization, account=job.account,
                direction=WhatsAppMessage.Direction.INBOUND,
            ).order_by("-created_at", "-id").first()
            reason = ""
            if source_payload.get("isHistory") is True and not hosted_job_allows_history(job):
                reason = "source_message_is_history"
            elif latest is None or latest.pk != job.source_message_id:
                reason = "superseded_by_newer_lead_message"
            if not reason:
                reason = hosted_ai_block_reason(account=job.account, lead=job.lead)
    else:
        from services.channels.hosted_whatsapp_service import account_ai_block_reason
        reason = account_ai_block_reason(account=job.account, lead=job.lead)
    if (
        reason == "whatsapp_account_not_connected"
        and job.account.connection_type == WhatsAppAccount.ConnectionType.coexisted
        and job.account.is_active
    ):
        # Connectivity is not revoked AI consent. Keep the already-generated
        # reply and recheck every control again after the gateway reconnects.
        return _defer(job, timezone.now() + timedelta(seconds=30),
                      "session_reconnecting", "Waiting for the Hosted session to reconnect.")
    if reason:
        _cancel_generated_message(job)
        finished = _finish(job, HostedAutomationJob.Status.SKIPPED, {**(job.result or {}), "status": "skipped", "reason": reason})
        return {"status": "skipped", "reason": reason} if finished.get("reason") == reason else finished

    if job.account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        pause_until = hosted_health_pause_until(account=job.account)
        if pause_until:
            return _requeue_for_health(job, pause_until)
    if not message_id:
        if job.kind == HostedAutomationJob.Kind.WELCOME:
            from services.channels.welcome_message_service import execute_queued_welcome
            result = execute_queued_welcome(job=job)
        else:
            from apps.hosted_automation.execution import execute_hosted_ai_engagement
            result = execute_hosted_ai_engagement(task=_DurableTask(job), job=job)
        job.result = {**(job.result or {}), **(result or {})}
        if not _owned(job).update(result=job.result):
            return {"status": "skipped", "reason": "job_lease_replaced"}
    result = job.result or {}
    if result.get("message_id"):
        delivery = _send_generated_ai_message(job)
        result = {**result, "delivery": delivery}
        status = HostedAutomationJob.Status.COMPLETED if delivery.get("status") == "sent" else HostedAutomationJob.Status.FAILED
    elif result.get("status") == "completed":
        status = HostedAutomationJob.Status.COMPLETED
    elif result.get("status") == "skipped":
        status = HostedAutomationJob.Status.SKIPPED
    else:
        status = HostedAutomationJob.Status.FAILED
    error = str(result.get("error") or result.get("reason") or (result.get("delivery") or {}).get("reason") or "") if status == HostedAutomationJob.Status.FAILED else ""
    return _finish(job, status, result, error)


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True,
             soft_time_limit=240, time_limit=270,
             name="apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task")
def process_hosted_ai_engagement_job_task(self, job_id):
    job, result = _claim(job_id)
    if job is None:
        return result
    try:
        return _execute(job)
    except HostedAutomationPaused as exc:
        reason = getattr(exc, "reason", "account_health_pause")
        return _defer(job, exc.paused_until, reason,
                      str(exc.__cause__ or exc) if reason in {"provider_transient", "provider_ack_pending", "session_reconnecting"} else "",
                      retry=reason == "provider_transient")
    except _DurableRetry as exc:
        return _defer(job, timezone.now() + timedelta(seconds=max(1, min(float(exc.countdown), 900))),
                      "retry_scheduled", str(exc), retry=True)
    except Exception as exc:
        logger.exception("AI job %s failed temporarily", job.pk)
        retries = int((job.result or {}).get("retry_count", 0))
        return _defer(job, timezone.now() + timedelta(seconds=min(300, 30 * (2 ** retries))),
                      "retry_scheduled", str(exc), retry=True)
