"""Install deterministic WhatsApp AI orchestration behavior.

This keeps channel transport code stable while replacing legacy eager summary
work, generic positive-reply stage movement, and long Hosted AI delay with the
precise SHVYA orchestration rules.
"""

from __future__ import annotations

import os

from services.channels.reply_intent_service import Intent, classify_reply


_INSTALLED = False


def _debounce_seconds() -> int:
    try:
        value = int(os.getenv("AI_ENGAGEMENT_DEBOUNCE_SECONDS", "5"))
    except (TypeError, ValueError):
        value = 5
    # Leave time for generation and delivery inside the hosted 30-second target.
    return min(max(value, 0), 5)


def _normalized_text(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def install_ai_orchestration_hooks() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from services.channels import hosted_automation_service, whatsapp_service

    def debounced_queue_engagement(*, lead_id):
        from apps.ai_engagement.tasks import generate_ai_engagement_response

        return generate_ai_engagement_response.apply_async(
            args=[str(lead_id)],
            countdown=0,
        )

    def background_summary_is_signal_owned(*, lead_id):
        # apps.ai_engagement.background_signals owns throttled summary and
        # qualification refreshes for both Meta and Hosted conversations.
        return None

    def deterministic_reply_intent(*, lead, body):
        """Never advance a CRM stage from a generic yes/interested keyword."""
        text = _normalized_text(body)
        if not text:
            return

        # A normal negative answer may be a qualification answer (for example
        # "No" to previous experience), so it must not disable AI or move a
        # stage. Preserve only the legacy review note for clearly negative
        # conversational intent.
        if classify_reply(body) == Intent.NEGATIVE:
            notes = lead.notes or ""
            marker = "[WhatsApp] Lead replied negatively -- needs review."
            if marker not in notes:
                lead.notes = f"{notes}\n{marker}".strip()
                lead.save(update_fields=["notes", "updated_at"])

    whatsapp_service._queue_whatsapp_engagement = debounced_queue_engagement
    whatsapp_service._queue_internal_conversation_summary = (
        background_summary_is_signal_owned
    )
    whatsapp_service._apply_reply_intent = deterministic_reply_intent

    # Hosted durable jobs already deduplicate by source message. Use the same
    # short debounce as Meta instead of the legacy one-minute response delay.
    hosted_automation_service.AI_RESPONSE_DELAY_SECONDS = _debounce_seconds()

    _INSTALLED = True
