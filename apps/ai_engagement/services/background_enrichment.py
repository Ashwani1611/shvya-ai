from __future__ import annotations

import os

from django.core.cache import cache

from apps.ai_engagement.models import InternalConversationSummary, OrgInfo
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead


DEFAULT_MESSAGE_INTERVAL = 6
SCHEDULE_LOCK_SECONDS = 20


def _message_interval() -> int:
    try:
        value = int(os.getenv("AI_ENRICHMENT_MESSAGE_INTERVAL", DEFAULT_MESSAGE_INTERVAL))
    except (TypeError, ValueError):
        value = DEFAULT_MESSAGE_INTERVAL
    return min(max(value, 2), 50)


def _messages_since_summary(*, lead) -> int:
    current = (
        InternalConversationSummary.objects.filter(
            organization=lead.organization,
            lead=lead,
            is_active=True,
        )
        .order_by("-generated_at", "-id")
        .first()
    )

    messages = WhatsAppMessage.objects.filter(
        organization=lead.organization,
        lead=lead,
    )
    if current is None:
        return messages.count()

    if current.source_last_message_at is not None:
        return messages.filter(created_at__gt=current.source_last_message_at).count()

    return max(messages.count() - int(current.source_message_count or 0), 0)


def enrichment_due(*, lead) -> bool:
    """Return whether enough new conversation exists for background AI work."""
    return _messages_since_summary(lead=lead) >= _message_interval()


def queue_background_enrichment(*, lead_id) -> dict:
    """Queue slow internal AI work without blocking a WhatsApp reply.

    Conversation summary and qualification summary run only after a bounded
    number of new messages, rather than on every inbound message. Lead briefing
    remains on-demand so it does not consume credits on every conversation.
    """
    lead = (
        Lead.objects.select_related("organization")
        .filter(id=lead_id)
        .first()
    )
    if lead is None:
        return {"status": "skipped", "reason": "lead_not_found"}

    if not enrichment_due(lead=lead):
        return {"status": "skipped", "reason": "interval_not_reached"}

    lock_key = f"shvya:ai:enrichment:schedule:{lead.id}"
    if not cache.add(lock_key, "1", timeout=SCHEDULE_LOCK_SECONDS):
        return {"status": "skipped", "reason": "already_scheduled"}

    from apps.ai_engagement.tasks import (
        generate_internal_conversation_summary,
        generate_lead_qualification,
    )

    generate_internal_conversation_summary.delay(str(lead.id))

    org_info = OrgInfo.objects.filter(organization=lead.organization).first()
    qualification_queued = bool(
        org_info and (org_info.qualification_requirements or "").strip()
    )
    if qualification_queued:
        # Qualification uses actual conversation as primary truth. A small
        # delay lets the rolling summary usually publish first without putting
        # either operation on the customer-response critical path.
        generate_lead_qualification.apply_async(
            args=[str(lead.id)],
            countdown=10,
        )

    return {
        "status": "queued",
        "summary": True,
        "qualification": qualification_queued,
    }
