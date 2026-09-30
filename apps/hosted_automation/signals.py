import logging

from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.hosted_automation.models import HostedAutomationJob

logger = logging.getLogger(__name__)


def schedule_hosted_ai_wakeup(available_at, account_id=None):
    """Publishing is a hint; the committed job survives any broker failure."""
    def publish():
        try:
            if account_id is not None:
                # Bulk imports and inbound bursts need one wake-up for the
                # sender, not thousands of duplicate dispatcher ETA tasks.
                # Losing this optional hint is safe: Beat scans the DB every
                # five seconds and no queue ownership depends on this key.
                bucket = int(available_at.timestamp() // 5)
                if not cache.add(f"shvya:ai-wakeup:{account_id}:{bucket}", "1", timeout=10):
                    return
            dispatch_due_hosted_ai.apply_async(
                countdown=max(0.0, (available_at - timezone.now()).total_seconds()),
            )
        except Exception:
            logger.exception("AI wake-up publish failed; the durable queue will recover")
    transaction.on_commit(publish)


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
    schedule_hosted_ai_wakeup(instance.available_at, getattr(instance, "account_id", None))


def _queue_hosted_ai_from_persisted_message(message_id, *, allow_history=False):
    """Canonical Hosted AI enqueue path from the final persisted inbound row.

    Historical sync remains suppressed by default. The authenticated inbox
    Create Lead action may explicitly activate exactly one linked source turn.
    """
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
    if payload.get("isHistory") and not allow_history:
        return

    account = message.account
    lead = message.lead

    from apps.ai_engagement.services.ai_permissions import (
        AIPermissionError,
        AIPermissionService,
    )
    from services.channels.hosted_automation_service import (
        EXPLICIT_LEAD_CREATION_AI_ACTIVATION,
        enqueue_ai_engagement,
    )
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
        # A temporary configuration read failure must not lose the inbound
        # turn. The durable worker rechecks every permission before sending.
        logger.exception("Could not evaluate inbound AI permissions; deferring to worker")
    else:
        if not permission.allowed:
            return

    # HostedAutomationJob.source_message is one-to-one, so repeated post-save
    # callbacks (initial save + LID identity repair) remain idempotent.
    enqueue_ai_engagement(
        account=account,
        lead=lead,
        source_message=message,
        activation=(
            EXPLICIT_LEAD_CREATION_AI_ACTIVATION
            if allow_history and payload.get("isHistory")
            else ""
        ),
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

        transaction.on_commit(apply_inbound_delay, robust=True)

    # Commit the job in the same transaction as the inbound record. Deferring
    # this database write until on_commit loses turns if a web worker exits
    # after saving the message. Identity repair saves the same row again and
    # the source-message uniqueness constraint makes that path idempotent.
    _queue_hosted_ai_from_persisted_message(instance.pk)
