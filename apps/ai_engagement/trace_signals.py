from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppMessage


@receiver(post_save, sender=WhatsAppMessage, dispatch_uid="ai_trace_delivery_status")
def update_ai_trace_delivery(sender, instance, **kwargs):
    if instance.direction != WhatsAppMessage.Direction.OUTBOUND:
        return
    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    metadata = (
        payload.get("shvya_ai")
        if isinstance(payload.get("shvya_ai"), dict)
        else {}
    )
    source_id = metadata.get("source_inbound_message_id")
    if not source_id:
        return

    transaction.on_commit(
        lambda: __import__(
            "apps.ai_engagement.services.trace_service",
            fromlist=["safe_delivery_update"],
        ).safe_delivery_update(
            organization_id=str(instance.organization_id),
            source_message_id=str(source_id),
            outbound_message=instance,
        )
    )
