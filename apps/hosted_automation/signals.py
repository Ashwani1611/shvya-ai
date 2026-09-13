from celery import shared_task
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.hosted_automation.models import HostedAutomationJob


@shared_task(name="hosted.dispatch_due_ai")
def dispatch_due_hosted_ai():
    """Wake the durable Hosted AI queue when a job reaches its due time."""
    from services.channels.hosted_automation_service import dispatch_one_hosted_ai_job

    return dispatch_one_hosted_ai_job()


@receiver(
    post_save,
    sender=HostedAutomationJob,
    dispatch_uid="hosted_automation_job_wakeup",
)
def hosted_automation_job_wakeup(sender, instance, created, update_fields=None, **kwargs):
    """Self-schedule queued Hosted AI at its exact configured due time."""
    if instance.status != HostedAutomationJob.Status.QUEUED:
        return

    if not created and update_fields is not None and "available_at" not in update_fields:
        return

    # ``enqueue_ai_engagement`` already owns the Hosted debounce. Do not subtract
    # a second processing budget here; doing so makes a 5-second debounce execute
    # immediately. Generation/delivery time is separate from the intentional
    # pre-generation debounce.
    delay_seconds = max(
        0.0,
        (instance.available_at - timezone.now()).total_seconds(),
    )

    transaction.on_commit(
        lambda delay=delay_seconds: dispatch_due_hosted_ai.apply_async(
            countdown=delay,
        )
    )


def _queue_hosted_ai_from_persisted_message(message_id):
    """Canonical Hosted AI enqueue path from the final persisted inbound row."""
    message = (
        WhatsAppMessage.objects.select_related(
            "account",
            "account__organization",
            "lead",
            "lead__organization",
            "lead__pipeline",
            "lead__stage",
        )
        .filter(
            pk=message_id,
            account__connection_type=WhatsAppAccount.ConnectionType.coexisted,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .first()
    )
    if message is None or message.lead_id is None:
        return

    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    if payload.get("isHistory"):
        return

    account = message.account
    lead = message.lead

    from apps.ai_engagement.services.ai_permissions import (
        AIPermissionError,
        AIPermissionService,
    )
    from services.channels.hosted_automation_service import enqueue_ai_engagement
    from services.channels.hosted_whatsapp_service import get_session_settings

    if not get_session_settings(account=account).get("ai_auto_reply"):
        return

    try:
        permission = AIPermissionService().evaluate(
            organization=account.organization,
            lead=lead,
            latest_inbound=message,
        )
    except AIPermissionError:
        return
    if not permission.allowed:
        return

    # HostedAutomationJob.source_message is one-to-one, so repeated post-save
    # callbacks (initial save + LID identity repair) remain idempotent.
    enqueue_ai_engagement(
        account=account,
        lead=lead,
        source_message=message,
    )


@receiver(post_save, sender=WhatsAppMessage, dispatch_uid="hosted_automation_message_state")
def hosted_message_state(sender, instance, created, update_fields=None, **kwargs):
    if instance.account.connection_type != WhatsAppAccount.ConnectionType.coexisted:
        return
    if instance.direction != WhatsAppMessage.Direction.INBOUND:
        return

    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    if payload.get("isHistory"):
        return

    if created and instance.lead_id:
        def apply_inbound_delay():
            from services.channels.hosted_automation_service import register_hosted_lead_reply

            message = (
                WhatsAppMessage.objects.select_related("account", "lead")
                .filter(pk=instance.pk)
                .first()
            )
            if message is None or message.lead_id is None:
                return
            register_hosted_lead_reply(
                account=message.account,
                lead=message.lead,
                at=message.created_at,
            )

        transaction.on_commit(apply_inbound_delay)

    # This signal is the single Hosted AI enqueue path. Always resolve and
    # permission-check the exact committed inbound row so another connected
    # number for the same Lead cannot steal this job's account context.
    transaction.on_commit(
        lambda message_id=instance.pk: _queue_hosted_ai_from_persisted_message(
            message_id
        )
    )
