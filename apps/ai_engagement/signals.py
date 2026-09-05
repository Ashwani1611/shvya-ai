from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppMessage


@receiver(post_save, sender=WhatsAppMessage)
def queue_ai_engagement_for_inbound_message(
    sender,
    instance: WhatsAppMessage,
    created: bool,
    **kwargs,
):
    """
    Queue AI engagement after a newly persisted inbound WhatsApp
    message commits successfully.

    This keeps the transport/service layer decoupled from the AI
    worker and preserves webhook idempotency: duplicate inbound
    deliveries do not create a second WhatsAppMessage, so they do
    not create a second engagement task.
    """
    if not created:
        return

    if instance.direction != WhatsAppMessage.Direction.INBOUND:
        return

    if not instance.lead_id:
        return

    lead_id = str(instance.lead_id)

    def _dispatch():
        from apps.ai_engagement.tasks import (
            generate_ai_engagement_response,
        )

        generate_ai_engagement_response.delay(lead_id)

    transaction.on_commit(_dispatch)
