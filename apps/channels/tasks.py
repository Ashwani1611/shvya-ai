"""
Celery tasks for outbound WhatsApp sends -- single messages and
bulk campaigns.

NOTE: Celery is scaffolded (config/celery.py) but not yet wired into
INSTALLED_APPS/config/__init__.py or given CELERY_BROKER_URL in
settings -- see the roadmap item "Add a Celery worker + beat service
to docker-compose.yml". Until that's done, calling
.delay()/.apply_async() on these tasks will raise, not silently
no-op. Write and reference them now so the send flow is correct the
moment that wiring lands.
"""
import logging

from celery import chord, group, shared_task

logger = logging.getLogger(__name__)


# ============================================================
# SINGLE MESSAGE SEND
# ============================================================


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def send_whatsapp_message_task(self, message_id):
    """
    Send an already-queued WhatsAppMessage via Meta's API.

    Retries on transient failures (network errors, Meta 5xx).
    Does NOT retry on WhatsAppSendError from a 4xx (bad token,
    invalid number, etc.) -- that's a permanent failure, already
    recorded on the message by the service layer.
    """
    from apps.channels.models import WhatsAppMessage
    from apps.channels.providers.whatsapp import WhatsAppAPIError
    from services.channels.whatsapp_service import (
        WhatsAppSendError,
        send_outbound_message,
    )

    try:
        message = WhatsAppMessage.objects.select_related("account").get(
            id=message_id,
        )

    except WhatsAppMessage.DoesNotExist:
        logger.warning(
            "send_whatsapp_message_task: message %s not found", message_id
        )
        return

    try:
        send_outbound_message(message=message)

    except WhatsAppSendError as exc:
        original = exc.__cause__

        # Retry only on network-level failures. A 4xx from Meta
        # (invalid token, bad number, etc.) will fail the same way
        # every time -- don't burn retries on it.
        if isinstance(original, WhatsAppAPIError) and (
            original.status_code is None or original.status_code >= 500
        ):
            raise self.retry(exc=exc)

        logger.error(
            "send_whatsapp_message_task: permanent failure for message %s: %s",
            message_id,
            exc,
        )


# ============================================================
# BULK CAMPAIGN SEND
# ============================================================


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=15,
    rate_limit="10/s",
)
def send_bulk_recipient_task(self, recipient_id):
    """
    Sends WhatsApp message to a single BulkMessageRecipient.

    rate_limit="10/s" throttles this across ALL workers processing
    this task type -- this is the mechanism that keeps a bulk
    campaign from blowing past Meta's messaging throughput limits,
    since individual recipient sends are dispatched all at once by
    send_bulk_campaign_task but only drained at 10/second.
    """
    from apps.channels.models import BulkMessageRecipient
    from apps.channels.providers.whatsapp import WhatsAppAPIError
    from services.channels.bulk_service import is_within_24h_window
    from services.channels.whatsapp_service import (
        WhatsAppSendError,
        queue_outbound_message,
        send_outbound_message,
    )

    try:
        recipient = (
            BulkMessageRecipient.objects.select_related(
                "campaign", "campaign__account", "lead"
            ).get(id=recipient_id)
        )

    except BulkMessageRecipient.DoesNotExist:
        logger.warning(
            "send_bulk_recipient_task: recipient %s not found", recipient_id
        )
        return

    campaign = recipient.campaign
    lead = recipient.lead

    if not is_within_24h_window(lead=lead) and not campaign.template_name:
        recipient.status = BulkMessageRecipient.Status.SKIPPED
        recipient.skip_reason = (
            "Outside 24h messaging window and no template configured."
        )
        recipient.save(update_fields=["status", "skip_reason", "updated_at"])
        return

    message = queue_outbound_message(
        organization=campaign.organization,
        account=campaign.account,
        to_number=lead.phone,
        body=campaign.body,
        lead=lead,
    )

    recipient.message = message
    recipient.save(update_fields=["message", "updated_at"])

    try:
        send_outbound_message(message=message)

    except WhatsAppSendError as exc:
        original = exc.__cause__

        if isinstance(original, WhatsAppAPIError) and (
            original.status_code is None or original.status_code >= 500
        ):
            raise self.retry(exc=exc)

        recipient.status = BulkMessageRecipient.Status.FAILED
        recipient.skip_reason = str(exc)
        recipient.save(update_fields=["status", "skip_reason", "updated_at"])
        return

    recipient.status = BulkMessageRecipient.Status.SENT
    recipient.save(update_fields=["status", "updated_at"])


@shared_task
def finalize_bulk_campaign_task(_results, campaign_id):
    """
    Runs once every send_bulk_recipient_task in the campaign's
    chord has finished (success or failure). Marks the campaign
    completed/failed based on how many recipients actually sent.
    """
    from apps.channels.models import BulkMessageCampaign
    from services.channels.bulk_service import mark_campaign_completed

    try:
        campaign = BulkMessageCampaign.objects.get(id=campaign_id)

    except BulkMessageCampaign.DoesNotExist:
        logger.warning(
            "finalize_bulk_campaign_task: campaign %s not found", campaign_id
        )
        return

    mark_campaign_completed(campaign=campaign)


@shared_task
def send_bulk_campaign_task(campaign_id):
    """
    Dispatcher: marks the campaign as sending, then fans out one
    send_bulk_recipient_task per pending recipient via a Celery
    chord, so finalize_bulk_campaign_task runs automatically once
    every recipient has been processed.
    """
    from apps.channels.models import BulkMessageCampaign, BulkMessageRecipient
    from services.channels.bulk_service import mark_campaign_started

    try:
        campaign = BulkMessageCampaign.objects.get(id=campaign_id)

    except BulkMessageCampaign.DoesNotExist:
        logger.warning(
            "send_bulk_campaign_task: campaign %s not found", campaign_id
        )
        return

    mark_campaign_started(campaign=campaign)

    recipient_ids = list(
        BulkMessageRecipient.objects.filter(
            campaign=campaign,
            status=BulkMessageRecipient.Status.PENDING,
        ).values_list("id", flat=True)
    )

    if not recipient_ids:
        finalize_bulk_campaign_task.delay(None, str(campaign.id))
        return

    chord(
        group(
            send_bulk_recipient_task.s(str(recipient_id))
            for recipient_id in recipient_ids
        )
    )(finalize_bulk_campaign_task.s(str(campaign.id)))