"""Hosted WhatsApp message-content repair and display helpers.

Identity repair lives in ``hosted_chat_service``. This module handles a
separate problem: an older row can already exist with an empty body or generic
``text`` type, while a later gateway/history payload has better content/type
metadata. Repairs are deliberately one-way: never overwrite an existing body
and never downgrade a known media type back to text.
"""

from __future__ import annotations

from apps.channels.models import WhatsAppMessage


_RAW_TYPE_LABELS = {
    "sticker": "Sticker",
    "revoked": "Message deleted",
    "reaction": "Reaction",
    "location": "Location",
    "live_location": "Live location",
    "contact_card": "Contact",
    "contacts_array": "Contacts",
    "poll_creation": "Poll",
    "poll_update": "Poll response",
    "buttons_response": "Button response",
    "list_response": "List response",
    "order": "Order",
    "call_log": "Call",
    "gp2": "WhatsApp system message",
    "notification_template": "WhatsApp notification",
    "e2e_notification": "WhatsApp security notification",
}

_MEDIA_TYPE_MAP = {
    "image": WhatsAppMessage.MessageType.IMAGE,
    "audio": WhatsAppMessage.MessageType.AUDIO,
    "ptt": WhatsAppMessage.MessageType.AUDIO,
    "video": WhatsAppMessage.MessageType.VIDEO,
    "document": WhatsAppMessage.MessageType.DOCUMENT,
}


def _payload(message):
    return message.raw_payload if isinstance(message.raw_payload, dict) else {}


def _clean(value):
    return str(value or "").strip()


def raw_message_type(payload):
    if not isinstance(payload, dict):
        return ""
    return _clean(payload.get("rawMessageType") or payload.get("messageType")).lower()


def resolved_message_type(payload):
    if not isinstance(payload, dict):
        return WhatsAppMessage.MessageType.TEXT

    declared = _clean(payload.get("messageType")).lower()
    allowed = {choice for choice, _label in WhatsAppMessage.MessageType.choices}
    if declared in allowed:
        return declared

    return _MEDIA_TYPE_MAP.get(
        raw_message_type(payload),
        WhatsAppMessage.MessageType.TEXT,
    )


def repair_gateway_message_content(*, message, payload, historical=False):
    """Repair only missing/less-specific content on an already persisted row."""
    if not message or not isinstance(payload, dict):
        return message

    current_payload = dict(_payload(message))
    incoming = {key: value for key, value in payload.items() if value is not None}
    merged = {**current_payload, **incoming}
    if historical or merged.get("isHistory"):
        merged["isHistory"] = True

    update_fields = []
    if merged != current_payload:
        message.raw_payload = merged
        update_fields.append("raw_payload")

    recovered_body = _clean(payload.get("body"))
    if not _clean(message.body) and recovered_body:
        message.body = recovered_body
        update_fields.append("body")

    recovered_type = resolved_message_type(payload)
    if (
        message.message_type == WhatsAppMessage.MessageType.TEXT
        and recovered_type != WhatsAppMessage.MessageType.TEXT
    ):
        message.message_type = recovered_type
        update_fields.append("message_type")

    if update_fields:
        update_fields.append("updated_at")
        message.save(update_fields=list(dict.fromkeys(update_fields)))

    return message


def repair_content_after_gateway_event(*, payload):
    """Repair content for live/history callbacks after normal persistence runs."""
    if not isinstance(payload, dict):
        return 0

    event = _clean(payload.get("event")).lower()
    items = []
    historical = False

    if event == "history_sync":
        items = [item for item in payload.get("messages") or [] if isinstance(item, dict)]
        historical = True
    elif event == "message":
        items = [payload]

    repaired = 0
    for item in items:
        message_id = _clean(item.get("messageId"))
        if not message_id:
            continue
        message = WhatsAppMessage.objects.filter(
            external_id=f"wweb:{message_id}"
        ).first()
        if not message:
            continue
        repair_gateway_message_content(
            message=message,
            payload=item,
            historical=historical,
        )
        repaired += 1

    return repaired


def display_text_for_message(message):
    """Return non-destructive UI text for body-less Hosted messages."""
    body = _clean(getattr(message, "body", ""))
    if body:
        return body

    message_type = _clean(getattr(message, "message_type", "")).lower()
    if message_type and message_type != WhatsAppMessage.MessageType.TEXT:
        try:
            return message.get_message_type_display()
        except (AttributeError, ValueError):
            return message_type.replace("_", " ").title()

    raw_type = raw_message_type(_payload(message))
    if raw_type in {"", "chat", "text"}:
        return "Message has no text content"
    if raw_type in _RAW_TYPE_LABELS:
        return _RAW_TYPE_LABELS[raw_type]
    if raw_type in _MEDIA_TYPE_MAP:
        return _MEDIA_TYPE_MAP[raw_type].replace("_", " ").title()
    return raw_type.replace("_", " ").title()


def decorate_hosted_chat_snapshot(snapshot):
    """Add display-only placeholders to body-less text rows in one snapshot.

    The model instances are changed only in memory for rendering/serialization;
    no placeholder text is persisted to ``WhatsAppMessage.body``.
    """
    for message in snapshot.get("thread") or []:
        if _clean(message.body):
            continue
        if message.message_type != WhatsAppMessage.MessageType.TEXT:
            continue
        message.body = display_text_for_message(message)
    return snapshot
