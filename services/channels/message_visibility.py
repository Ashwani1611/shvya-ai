"""Shared inbox boundary: automatic work becomes a chat message after sending."""

from django.db.models import Q


def pending_ai_message_q(prefix=""):
    # Cancelling an unsent draft must not turn it into a new failed bubble.
    # Provider failures remain visible unless they are explicitly identified
    # as pre-send cancellation and have no recorded send evidence.
    cancelled_draft = Q(**{
        f"{prefix}status": "failed",
        f"{prefix}sent_at__isnull": True,
    }) & (
        Q(**{f"{prefix}external_id__isnull": True})
        | Q(**{f"{prefix}external_id": ""})
    ) & (
        Q(**{f"{prefix}error__istartswith": "AI send cancelled"})
        | Q(**{f"{prefix}error__istartswith": "Superseded"})
    )
    return Q(**{f"{prefix}direction": "outbound"}) & (
        Q(**{f"{prefix}status__in": ["queued", "sending"]}) | cancelled_draft
    ) & (
        Q(**{f"{prefix}raw_payload__has_key": "shvya_ai"})
        | Q(**{f"{prefix}raw_payload__has_key": "shvya_welcome"})
    )
