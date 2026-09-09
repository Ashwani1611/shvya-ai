from __future__ import annotations

import os

from django.core.cache import cache

from apps.ai_engagement.models import InternalConversationSummary, OrgInfo
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead


DEFAULT_MESSAGE_INTERVAL = 6
DEFAULT_CHAR_INTERVAL = 2400
SCHEDULE_LOCK_SECONDS = 20


def _message_interval() -> int:
    try:
        value = int(os.getenv("AI_ENRICHMENT_MESSAGE_INTERVAL", DEFAULT_MESSAGE_INTERVAL))
    except (TypeError, ValueError):
        value = DEFAULT_MESSAGE_INTERVAL
    return min(max(value, 2), 50)


def _char_interval() -> int:
    try:
        value = int(os.getenv("AI_ENRICHMENT_CHAR_INTERVAL", DEFAULT_CHAR_INTERVAL))
    except (TypeError, ValueError):
        value = DEFAULT_CHAR_INTERVAL
    return min(max(value, 800), 12000)


def _new_message_stats(*, lead) -> tuple[int, int]:
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
    if current is not None:
        if current.source_last_message_at is not None:
            messages = messages.filter(created_at__gt=current.source_last_message_at)
        elif current.source_message_count:
            # Legacy summaries may not have source_last_message_at. Message-count
            # compatibility is retained; char-based triggering starts fresh once
            # the next summary stores a timestamp.
            total_count = messages.count()
            count = max(total_count - int(current.source_message_count or 0), 0)
            recent = messages.order_by("-created_at", "-id")[:count]
            chars = sum(len(str(body or "")) for body in recent.values_list("body", flat=True))
            return count, chars

    bodies = list(messages.values_list("body", flat=True))
    return len(bodies), sum(len(str(body or "")) for body in bodies)


def _messages_since_summary(*, lead) -> int:
    # Public compatibility helper used by older tests/callers.
    return _new_message_stats(lead=lead)[0]


def enrichment_due(*, lead) -> bool:
    """Trigger only when enough new semantic payload exists.

    Either several short WhatsApp turns or a smaller number of long substantive
    messages can refresh the rolling summary. This avoids spending a summary
    model call on six trivial acknowledgements while preventing long turns from
    overflowing the recent-context window.
    """
    count, chars = _new_message_stats(lead=lead)
    if chars >= _char_interval():
        return True
    if count < _message_interval():
        return False

    # Six tiny acknowledgements generally do not justify a summary call.
    average_chars = chars / max(count, 1)
    return chars >= 180 or average_chars >= 30


def queue_background_enrichment(*, lead_id, force=False) -> dict:
    """Queue slow internal AI work without blocking a WhatsApp reply."""
    lead = (
        Lead.objects.select_related("organization")
        .filter(id=lead_id)
        .first()
    )
    if lead is None:
        return {"status": "skipped", "reason": "lead_not_found"}

    if not force and not enrichment_due(lead=lead):
        from apps.ai_engagement.tasks import flush_background_enrichment

        # A final short answer must eventually reach the summary and notes even
        # if the customer never sends another message. Coalesce bursts once.
        key = f"shvya:ai:enrichment:flush:{lead.id}"
        if cache.add(key, "1", timeout=20):
            try:
                flush_background_enrichment.apply_async(args=[str(lead.id)], countdown=20)
            except Exception:
                cache.delete(key)
                raise
        return {"status": "queued", "reason": "deferred_flush"}

    lock_key = f"shvya:ai:enrichment:schedule:{lead.id}"
    if not cache.add(lock_key, "1", timeout=SCHEDULE_LOCK_SECONDS):
        return {"status": "skipped", "reason": "already_scheduled"}

    from apps.ai_engagement.tasks import (
        generate_internal_conversation_summary,
        generate_lead_qualification,
    )

    try:
        generate_internal_conversation_summary.delay(str(lead.id))
    except Exception:
        cache.delete(lock_key)
        raise

    org_info = OrgInfo.objects.filter(organization=lead.organization).first()
    qualification_queued = bool(
        org_info and (org_info.qualification_requirements or "").strip()
    )
    if qualification_queued:
        generate_lead_qualification.apply_async(
            args=[str(lead.id)],
            countdown=10,
        )

    return {
        "status": "queued",
        "summary": True,
        "qualification": qualification_queued,
    }
