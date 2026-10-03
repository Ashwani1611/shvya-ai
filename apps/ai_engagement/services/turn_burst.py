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
