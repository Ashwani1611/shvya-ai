"""Realtime event publishing for the Meta Cloud API inbox."""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction


def _message_payload(message):
    return {
        "id": str(message.id),
        "lead_id": str(message.lead_id) if message.lead_id else None,
        "direction": message.direction,
        "body": message.body or "",
        "message_type": message.message_type,
        "status": message.status,
        "created_at": message.created_at.isoformat(),
    }


def publish_message(message):
    """Publish a committed message to its open thread and org inbox."""
    if not message.lead_id:
        return
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    payload = _message_payload(message)
    async_to_sync(channel_layer.group_send)(
        f"whatsapp_thread_{message.lead_id}",
        {"type": "whatsapp.message", "message": payload},
    )
    async_to_sync(channel_layer.group_send)(
        f"whatsapp_inbox_{message.organization_id}",
        {
            "type": "whatsapp.inbox_update",
            "lead_id": str(message.lead_id),
            "unread_count": 0 if message.direction == "outbound" else 1,
            "last_message_at": message.created_at.isoformat(),
            "last_message_preview": message.body or "",
        },
    )


def publish_status(message):
    if not message.lead_id:
        return
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f"whatsapp_thread_{message.lead_id}",
        {
            "type": "whatsapp.status",
            "message_id": str(message.id),
            "lead_id": str(message.lead_id),
            "status": message.status,
        },
    )


def queue_message_publish(message):
    transaction.on_commit(lambda message=message: publish_message(message))


def queue_status_publish(message):
    transaction.on_commit(lambda message=message: publish_status(message))
