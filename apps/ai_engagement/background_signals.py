"""Non-blocking internal AI enrichment hooks.

Inbound WhatsApp engagement itself is queued by the channel services. This
module queues only throttled internal summary/qualification work after commit.
"""

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.ai_engagement.services.background_enrichment import (
    queue_background_enrichment,
)
from apps.channels.models import WhatsAppMessage


@receiver(post_save, sender=WhatsAppMessage)
def queue_internal_ai_enrichment(sender, instance, created, **kwargs):
    if not created or not instance.lead_id:
        return
    if instance.direction != WhatsAppMessage.Direction.INBOUND:
        return

    lead_id = str(instance.lead_id)
    transaction.on_commit(
        lambda lead_id=lead_id: queue_background_enrichment(
            lead_id=lead_id,
        )
    )
