from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppMessage
from apps.ai_engagement.services.intent_runtime import install_intent_runtime
from apps.ai_engagement.services.phase5_6_safety_fixes import (
    install_phase5_6_safety_fixes,
)


# Phase 5/6 audit safeguards stay inside the canonical shared runtime. They
# tighten evidence authority, reuse backend-owned CRM/qualification truth for
# memory, and skip the second grounding model call only on provably low-risk or
# extractively grounded replies. No transport-specific reasoning path is added.
install_phase5_6_safety_fixes()

# Phase 2 customer-message understanding is installed immediately before the
# Phase 1 trace runtime wraps the shared context builder. This keeps one Intent
# Engine for Meta API, Coexistence and Hosted WhatsApp while allowing Phase 1
# observability to record the resulting IntentDecision without owning policy.
install_intent_runtime()


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
