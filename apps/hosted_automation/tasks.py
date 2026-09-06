import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.hosted_automation.models import HostedAutomationJob
from services.channels.hosted_automation_service import automation_pause_until


logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task",
)
def process_hosted_ai_engagement_job_task(self, job_id):
    """Execute the durable ~60-second Hosted Account AI job."""
    try:
        job = (
            HostedAutomationJob.objects.select_related(
                "account", "organization", "lead", "source_message"
            ).get(id=job_id)
        )
    except HostedAutomationJob.DoesNotExist:
        return {"status": "skipped", "reason": "job_not_found"}

    if job.status not in {
        HostedAutomationJob.Status.PROCESSING,
        HostedAutomationJob.Status.QUEUED,
    }:
        return {"status": "skipped", "reason": "job_already_finished"}

    latest = (
        job.lead.whatsapp_messages.filter(
            organization=job.organization,
            account=job.account,
            direction="inbound",
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if not latest or latest.id != job.source_message_id:
        job.status = HostedAutomationJob.Status.SKIPPED
        job.completed_at = timezone.now()
        job.result = {"reason": "superseded_by_newer_lead_message"}
        job.save(update_fields=["status", "completed_at", "result", "updated_at"])
        return {"status": "skipped", "reason": "superseded_by_newer_lead_message"}

    pause_until = automation_pause_until(account=job.account)
    if pause_until:
        job.status = HostedAutomationJob.Status.QUEUED
        job.available_at = pause_until
        job.started_at = None
        job.save(update_fields=["status", "available_at", "started_at", "updated_at"])
        return {
            "status": "deferred",
            "reason": "account_health_pause",
            "available_at": pause_until.isoformat(),
        }

    from apps.ai_engagement.tasks import _execute_ai_engagement_response

    try:
        result = _execute_ai_engagement_response(task=self, lead_id=str(job.lead_id))
    except Exception as exc:
        # Celery Retry is intentionally re-raised by self.retry inside the
        # canonical AI path. For other failures, preserve durable job state.
        from celery.exceptions import Retry

        if isinstance(exc, Retry):
            raise
        logger.exception("Hosted AI engagement job %s failed", job_id)
        job.status = HostedAutomationJob.Status.FAILED
        job.completed_at = timezone.now()
        job.error = str(exc)
        job.save(update_fields=["status", "completed_at", "error", "updated_at"])
        return {"status": "failed", "error": str(exc)}

    final_status = str((result or {}).get("status") or "failed")
    if final_status == "completed":
        model_status = HostedAutomationJob.Status.COMPLETED
    elif final_status == "skipped":
        model_status = HostedAutomationJob.Status.SKIPPED
    else:
        model_status = HostedAutomationJob.Status.FAILED

    with transaction.atomic():
        locked = HostedAutomationJob.objects.select_for_update().get(id=job.id)
        locked.status = model_status
        locked.completed_at = timezone.now()
        locked.result = result or {}
        if model_status == HostedAutomationJob.Status.FAILED:
            locked.error = str((result or {}).get("error") or (result or {}).get("reason") or "AI engagement failed")
        locked.save(
            update_fields=["status", "completed_at", "result", "error", "updated_at"]
        )
    return result
