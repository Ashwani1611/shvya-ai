
# Add Celery tasks for channels here
"""
Celery tasks for outbound WhatsApp sends.

NOTE: Celery is scaffolded (config/celery.py) but not yet wired into
INSTALLED_APPS/config/__init__.py or given CELERY_BROKER_URL in
settings -- see the roadmap item "Add a Celery worker + beat service
to docker-compose.yml". Until that's done, calling
.delay()/.apply_async() on this task will not actually run it
asynchronously. Write and reference it now so the send flow is
correct the moment that wiring lands.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


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