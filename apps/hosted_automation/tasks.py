import logging
from contextlib import contextmanager

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.hosted_automation.models import HostedAutomationJob
from services.channels.hosted_automation_service import (
    HostedAutomationPaused,
    automation_pause_until,
    hosted_ai_block_reason,
)


logger = logging.getLogger(__name__)


def _requeue_for_health(job, paused_until):
    job.status = HostedAutomationJob.Status.QUEUED
    job.available_at = paused_until
    job.started_at = None
    job.save(update_fields=["status", "available_at", "started_at", "updated_at"])
    return {
        "status": "deferred",
        "reason": "account_health_pause",
        "available_at": paused_until.isoformat(),
    }


def _cancel_generated_message(job):
    message_id = (job.result or {}).get("message_id")
    if not message_id:
        return
    WhatsAppMessage.objects.filter(
        id=message_id,
        status=WhatsAppMessage.Status.QUEUED,
    ).update(
        status=WhatsAppMessage.Status.FAILED,
        error="Superseded by a newer lead message before Hosted AI delivery.",
    )


def _send_generated_ai_message(job):
    message_id = (job.result or {}).get("message_id")
    if not message_id:
        return None
    try:
        message = WhatsAppMessage.objects.select_related("account", "lead").get(
            id=message_id,
            organization=job.organization,
            account=job.account,
        )
    except WhatsAppMessage.DoesNotExist:
        return {"status": "failed", "reason": "generated_message_missing"}
    if message.status in {
        WhatsAppMessage.Status.SENT,
        WhatsAppMessage.Status.DELIVERED,
        WhatsAppMessage.Status.READ,
    }:
        return {"status": "sent", "message_id": str(message.id)}
    if message.status != WhatsAppMessage.Status.QUEUED:
        return {
            "status": "failed",
            "reason": "generated_message_not_queued",
            "message_id": str(message.id),
        }

    from services.channels.hosted_whatsapp_transport import send_hosted_message
    from services.channels.whatsapp_service import WhatsAppSendError

    try:
        send_hosted_message(message=message, defer_on_pause=True)
    except WhatsAppSendError as exc:
        return {"status": "failed", "reason": str(exc), "message_id": str(message.id)}
    return {"status": "sent", "message_id": str(message.id)}


@contextmanager
def _hosted_ai_execution_scope(job):
    """Keep the shared AI engine pinned to this Hosted conversation.

    The canonical AI engine is intentionally lead-centric. A lead can have
    WhatsApp history on more than one connected number, so every latest-message,
    account, context, duplicate, and permission lookup is scoped to the durable
    Hosted job's exact account while this single-worker task executes.
    """
    from apps.ai_engagement import tasks as ai_tasks
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.ai_engagement.services.context import AIContextBuilder
    from services.channels import whatsapp_service

    original_latest = ai_tasks._latest_whatsapp_message
    original_existing = ai_tasks._has_existing_ai_response
    original_resolver = whatsapp_service.resolve_account_for_lead
    original_get_messages = AIContextBuilder._get_messages
    original_permission_latest = AIPermissionService._latest_inbound_message

    def hosted_latest(*, lead):
        return (
            lead.whatsapp_messages.filter(
                organization=job.organization,
                account_id=job.account_id,
            )
            .order_by("-created_at", "-id")
            .first()
        )

    def hosted_existing(*, lead, inbound_message, body):
        if inbound_message is None:
            return False
        source_id = str(inbound_message.id)
        outbound = WhatsAppMessage.objects.filter(
            organization=job.organization,
            account_id=job.account_id,
            lead=lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
        )
        if outbound.filter(
            raw_payload__shvya_ai__source_inbound_message_id=source_id,
        ).exists():
            return True
        if not body:
            return False
        return outbound.filter(
            body=body,
            created_at__gte=inbound_message.created_at,
        ).exists()

    def hosted_resolver(*, organization, lead):
        if organization.pk != job.organization_id or lead.pk != job.lead_id:
            return None
        return WhatsAppAccount.objects.filter(
            pk=job.account_id,
            organization=organization,
            connection_type="hosted",
            is_active=True,
            status=WhatsAppAccount.Status.CONNECTED,
        ).first()

    def hosted_get_messages(builder, *, organization, lead, limit):
        if organization.pk != job.organization_id or lead.pk != job.lead_id:
            return original_get_messages(
                builder,
                organization=organization,
                lead=lead,
                limit=limit,
            )
        messages = list(
            WhatsAppMessage.objects.filter(
                organization=organization,
                account_id=job.account_id,
                lead=lead,
            )
            .order_by("-created_at", "-id")[:limit]
        )
        messages.reverse()
        return messages

    def hosted_permission_latest(permission_service, *, organization, lead):
        if organization.pk != job.organization_id or lead.pk != job.lead_id:
            return original_permission_latest(
                permission_service,
                organization=organization,
                lead=lead,
            )
        return (
            lead.whatsapp_messages.filter(
                organization=organization,
                account_id=job.account_id,
                direction=WhatsAppMessage.Direction.INBOUND,
            )
            .select_related("account")
            .order_by("-created_at", "-id")
            .first()
        )

    ai_tasks._latest_whatsapp_message = hosted_latest
    ai_tasks._has_existing_ai_response = hosted_existing
    whatsapp_service.resolve_account_for_lead = hosted_resolver
    AIContextBuilder._get_messages = hosted_get_messages
    AIPermissionService._latest_inbound_message = hosted_permission_latest
    try:
        yield
    finally:
        AIPermissionService._latest_inbound_message = original_permission_latest
        AIContextBuilder._get_messages = original_get_messages
        whatsapp_service.resolve_account_for_lead = original_resolver
        ai_tasks._has_existing_ai_response = original_existing
        ai_tasks._latest_whatsapp_message = original_latest


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task",
)
def process_hosted_ai_engagement_job_task(self, job_id):
    """Execute one durable Hosted Account AI job."""
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
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if not latest or latest.id != job.source_message_id:
        _cancel_generated_message(job)
        job.status = HostedAutomationJob.Status.SKIPPED
        job.completed_at = timezone.now()
        job.result = {**(job.result or {}), "reason": "superseded_by_newer_lead_message"}
        job.save(update_fields=["status", "completed_at", "result", "updated_at"])
        return {"status": "skipped", "reason": "superseded_by_newer_lead_message"}

    reason = hosted_ai_block_reason(account=job.account, lead=job.lead)
    if reason:
        _cancel_generated_message(job)
        job.status = HostedAutomationJob.Status.SKIPPED
        job.completed_at = timezone.now()
        job.result = {**(job.result or {}), "reason": reason}
        job.save(update_fields=["status", "completed_at", "result", "updated_at"])
        return {"status": "skipped", "reason": reason}

    pause_until = automation_pause_until(account=job.account)
    if pause_until:
        return _requeue_for_health(job, pause_until)

    # A previous run may already have generated the outbound message before
    # Account Health paused. Resume that exact queued message instead of
    # regenerating AI text after the 12-hour cooldown.
    if (job.result or {}).get("message_id"):
        try:
            send_result = _send_generated_ai_message(job)
        except HostedAutomationPaused as exc:
            return _requeue_for_health(job, exc.paused_until)
        if send_result and send_result.get("status") == "sent":
            job.status = HostedAutomationJob.Status.COMPLETED
            job.completed_at = timezone.now()
            job.result = {**(job.result or {}), "delivery": send_result}
            job.save(update_fields=["status", "completed_at", "result", "updated_at"])
            return job.result
        job.status = HostedAutomationJob.Status.FAILED
        job.completed_at = timezone.now()
        job.error = str((send_result or {}).get("reason") or "Hosted AI delivery failed")
        job.save(update_fields=["status", "completed_at", "error", "updated_at"])
        return send_result

    from apps.ai_engagement.tasks import _execute_ai_engagement_response

    try:
        with _hosted_ai_execution_scope(job):
            result = _execute_ai_engagement_response(task=self, lead_id=str(job.lead_id))
    except Exception as exc:
        from celery.exceptions import Retry

        if isinstance(exc, Retry):
            raise
        logger.exception("Hosted AI engagement job %s failed", job_id)
        job.status = HostedAutomationJob.Status.FAILED
        job.completed_at = timezone.now()
        job.error = str(exc)
        job.save(update_fields=["status", "completed_at", "error", "updated_at"])
        return {"status": "failed", "error": str(exc)}

    result = result or {}
    # Persist the generated message id before attempting delivery. If health
    # flips to paused at the limit, this exact message survives the cooldown.
    job.result = result
    job.save(update_fields=["result", "updated_at"])

    final_status = str(result.get("status") or "failed")
    if final_status == "completed" and result.get("engaged") and result.get("message_id"):
        try:
            send_result = _send_generated_ai_message(job)
        except HostedAutomationPaused as exc:
            return _requeue_for_health(job, exc.paused_until)
        if send_result and send_result.get("status") == "sent":
            result = {**result, "delivery": send_result}
            model_status = HostedAutomationJob.Status.COMPLETED
        else:
            result = {**result, "delivery": send_result or {}}
            model_status = HostedAutomationJob.Status.FAILED
    elif final_status == "completed":
        model_status = HostedAutomationJob.Status.COMPLETED
    elif final_status == "skipped":
        model_status = HostedAutomationJob.Status.SKIPPED
    else:
        model_status = HostedAutomationJob.Status.FAILED

    with transaction.atomic():
        locked = HostedAutomationJob.objects.select_for_update().get(id=job.id)
        locked.status = model_status
        locked.completed_at = timezone.now()
        locked.result = result
        if model_status == HostedAutomationJob.Status.FAILED:
            locked.error = str(
                result.get("error")
                or result.get("reason")
                or (result.get("delivery") or {}).get("reason")
                or "AI engagement failed"
            )
        locked.save(
            update_fields=["status", "completed_at", "result", "error", "updated_at"]
        )
    return result
