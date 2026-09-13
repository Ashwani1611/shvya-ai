from __future__ import annotations

import re
from dataclasses import replace


_INSTALLED = False
_BOOKING_CLAIM_RE = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?(?:already\s+)?(?:booked|scheduled)\b",
    flags=re.IGNORECASE,
)
_MISSED_CALL_TERMS = (
    "no one called",
    "nobody called",
    "didn't call",
    "did not call",
    "missed my call",
)


def install_natural_conversation_booking_guard() -> None:
    """Do not turn a reported booking into a new callback/reminder request."""

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import engagement_failsoft as failsoft

    current_builder = failsoft.build_deterministic_fallback_decision

    def build_deterministic_fallback_decision(*, organization, lead, latest_inbound=None):
        source = latest_inbound
        if source is None:
            source = failsoft._latest_inbound_for_lead(
                organization=organization,
                lead=lead,
            )
        latest_text = str(getattr(source, "body", "") or "").strip()
        normalized = latest_text.casefold()
        booking_claim = bool(_BOOKING_CLAIM_RE.search(latest_text))
        missed_call = any(term in normalized for term in _MISSED_CALL_TERMS)

        decision = current_builder(
            organization=organization,
            lead=lead,
            latest_inbound=source,
        )
        if not booking_claim or missed_call:
            return decision

        return replace(
            decision,
            message=(
                "Got it. I’ll treat that as a booking you’ve reported, but I can’t "
                "confirm the appointment unless the booking system confirms it."
            ),
            reason="NORMAL_CONVERSATION",
            reason_code="NORMAL_CONVERSATION",
            crm_actions=[],
        )

    failsoft.build_deterministic_fallback_decision = build_deterministic_fallback_decision
    _INSTALLED = True
