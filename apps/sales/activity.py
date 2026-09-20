from __future__ import annotations

from apps.sales.models_lifecycle import SalesActivity


def record_activity(
    document,
    *,
    event_type,
    message,
    actor=None,
    metadata=None,
):
    """Append one tenant-scoped immutable sales activity event."""
    safe_metadata = metadata if isinstance(metadata, dict) else {}
    # Never persist credentials or raw provider payloads in this audit stream.
    blocked = {
        "secret",
        "password",
        "token",
        "access_token",
        "webhook_secret",
        "raw_payload",
        "credentials",
    }
    safe_metadata = {
        str(key): value
        for key, value in safe_metadata.items()
        if str(key).lower() not in blocked
    }
    return SalesActivity.objects.create(
        organization=document.organization,
        document=document,
        actor=actor,
        event_type=str(event_type or "")[:64],
        message=str(message or "")[:500],
        metadata=safe_metadata,
    )
