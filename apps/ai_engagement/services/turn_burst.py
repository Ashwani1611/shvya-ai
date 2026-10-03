from __future__ import annotations

import os


DEFAULT_TURN_BURST_SECONDS = 4
MAX_TURN_BURST_SECONDS = 5


def turn_burst_seconds() -> int:
    """Short quiet-window used to coalesce rapid customer messages.

    Multiple broker tasks may still be published, but existing latest-message,
    durable execution and generation locks ensure only the newest conversation
    revision can produce a reply after this window.
    """

    raw = (
        os.getenv("AI_TURN_BURST_SECONDS")
        or os.getenv("AI_ENGAGEMENT_DEBOUNCE_SECONDS")
        or str(DEFAULT_TURN_BURST_SECONDS)
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TURN_BURST_SECONDS
    return min(max(value, 0), MAX_TURN_BURST_SECONDS)


def defer_api_turn_until_quiet(*, task, lead_id: str) -> dict | None:
    """Requeue a lead until the newest inbound has been quiet for the burst window."""

    from datetime import timedelta

    from django.utils import timezone

    from apps.channels.models import WhatsAppMessage

    source = (
        WhatsAppMessage.objects.filter(
            lead_id=lead_id,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if source is None or source.created_at is None:
        return None

    execution = (
        (source.raw_payload or {}).get("shvya_ai_execution")
        if isinstance(source.raw_payload, dict)
        else {}
    ) or {}
    burst = turn_burst_seconds()
    if burst <= 0 or execution.get("status") not in {"queued", "retrying"}:
        return None

    remaining = (
        source.created_at + timedelta(seconds=burst) - timezone.now()
    ).total_seconds()
    if remaining <= 0:
        return None

    task.apply_async(
        args=[str(lead_id)],
        countdown=max(1, min(burst, int(remaining + 0.999))),
    )
    return {
        "status": "deferred",
        "reason": "conversation_burst_active",
        "lead_id": str(lead_id),
        "source_message_id": str(source.pk),
    }
