"""
Classifies an inbound WhatsApp reply as positive/negative/neutral
so the CRM can auto-advance a Lead's pipeline stage without a human
reading every message.

This is intentionally simple keyword matching, not an LLM call --
per CLAUDE.md rule 3, an LLM call is expensive and belongs in a
Celery task if/when it's added (see services.ai, currently
unbuilt). Keyword matching runs synchronously inside the webhook
handler because it's cheap and needs to be fast.
"""
import re

POSITIVE_PATTERNS = [
    r"^\s*yes\b",
    r"^\s*yep\b",
    r"^\s*yeah\b",
    r"^\s*sure\b",
    r"^\s*ok(ay)?\b",
    r"^\s*interested\b",
    r"^\s*\+\s*$",
    r"^\s*👍",
]

NEGATIVE_PATTERNS = [
    r"^\s*no\b",
    r"^\s*nope\b",
    r"^\s*not interested\b",
    r"^\s*stop\b",
    r"^\s*unsubscribe\b",
    r"^\s*-\s*$",
]

_POSITIVE_RE = re.compile("|".join(POSITIVE_PATTERNS), re.IGNORECASE)
_NEGATIVE_RE = re.compile("|".join(NEGATIVE_PATTERNS), re.IGNORECASE)


class Intent:
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


def classify_reply(body):
    """
    Returns one of Intent.POSITIVE / Intent.NEGATIVE / Intent.NEUTRAL.

    Neutral means "don't auto-advance or auto-drop the lead" -- a
    human (or, later, an AI copilot reply) should look at it.
    """
    if not body:
        return Intent.NEUTRAL

    text = body.strip()

    if _POSITIVE_RE.match(text):
        return Intent.POSITIVE

    if _NEGATIVE_RE.match(text):
        return Intent.NEGATIVE

    return Intent.NEUTRAL