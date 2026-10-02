"""Inbox visibility without confusing queued Hosted work with successful sends."""

from django.db.models import Q


def pending_ai_message_q(prefix=""):
    # Hosted bubbles show queued/sending work with explicit pending labels.
    # Keep the existing Cloud API visibility policy and hide superseded drafts
    # for both providers; a real provider failure must remain inspectable.
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
    pending_non_hosted = Q(**{
        f"{prefix}status__in": ["queued", "sending"],
    }) & ~Q(**{f"{prefix}account__connection_type": "hosted"})
    return Q(**{f"{prefix}direction": "outbound"}) & (
        pending_non_hosted | cancelled_draft
    ) & (
        Q(**{f"{prefix}raw_payload__has_key": "shvya_ai"})
        | Q(**{f"{prefix}raw_payload__has_key": "shvya_welcome"})
    )
