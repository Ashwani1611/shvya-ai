from celery import shared_task
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.channels.models import WhatsAppMessage
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
    """Self-schedule queued Hosted AI instead of relying only on Celery Beat.

    Beat remains the recovery scanner and priority coordinator, while this
    one-shot wake-up makes each queued AI job executable at ``available_at``
    even if the periodic dispatcher is delayed. The dispatcher still performs
    the lock, live permission checks, supersession checks, and AI-before-
    sequence claim, so this cannot bypass queue safeguards.
    """
    if instance.status != HostedAutomationJob.Status.QUEUED:
        return

    # Reschedule only when the job was created or its due time changed, such as
    # an Account Health pause. Other queued-field saves must not fan out tasks.
    if not created and update_fields is not None and "available_at" not in update_fields:
        return

    delay_seconds = max(
        0.0,
        (instance.available_at - timezone.now()).total_seconds(),
    )

    transaction.on_commit(
        lambda delay=delay_seconds: dispatch_due_hosted_ai.apply_async(
            countdown=delay,
        )
    )


@receiver(post_save, sender=WhatsAppMessage, dispatch_uid="hosted_automation_message_state")
def hosted_message_state(sender, instance, created, **kwargs):
    if not created or instance.account.connection_type != "hosted" or not instance.lead_id:
        return

    if instance.direction == WhatsAppMessage.Direction.INBOUND:
        def apply_inbound_delay():
            from services.channels.hosted_automation_service import register_hosted_lead_reply

            register_hosted_lead_reply(
                account=instance.account,
                lead=instance.lead,
                at=instance.created_at,
            )

        transaction.on_commit(apply_inbound_delay)
