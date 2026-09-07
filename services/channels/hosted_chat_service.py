"""Hosted WhatsApp chat read-model, identity repair, and realtime notifications.

The linked-device gateway is the source of WhatsApp chat identity.  Current
WhatsApp Web can expose direct chats as ``@lid`` ids, so a LID must never be
mistaken for a phone number.  The gateway sends ``peerKey`` / ``peerPhone``
metadata and this module keeps persisted WhatsAppMessage rows aligned with
that canonical identity.

This module deliberately wraps the existing hosted WhatsApp service rather
than duplicating its lead/automation rules.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead
from services.channels.hosted_whatsapp_service import (
    handle_gateway_event as legacy_handle_gateway_event,
    normalize_whatsapp_number,
)


MAX_CONVERSATION_SCAN = 10000
MAX_CONVERSATIONS = 250
MAX_THREAD_MESSAGES = 1000


def _payload(message):
    return message.raw_payload if isinstance(message.raw_payload, dict) else {}


def _raw_id(value):
    return str(value or "").strip()


def _phone_from_value(value):
    """Return +digits only for phone-like values, never for a WhatsApp LID."""
    raw = _raw_id(value)
    if not raw or raw.endswith("@lid"):
        return ""
    if "@" in raw and not raw.endswith("@c.us"):
        return ""
    if raw.endswith("@c.us"):
        raw = raw.split("@", 1)[0]
    return normalize_whatsapp_number(phone_number=raw)


def chat_key_for_message(message):
    """Canonical conversation key used by the Hosted Chats UI."""
    payload = _payload(message)

    peer_key = _raw_id(payload.get("peerKey"))
    if peer_key:
        if peer_key.endswith("@lid"):
            peer_phone = _phone_from_value(payload.get("peerPhone"))
            return peer_phone or peer_key
        return peer_key

    peer_phone = _phone_from_value(payload.get("peerPhone"))
    if peer_phone:
        return peer_phone

    if payload.get("isGroup"):
        return _raw_id(payload.get("chatId")) or message.from_number

    candidate = (
        message.from_number
        if message.direction == WhatsAppMessage.Direction.INBOUND
        else message.to_number
    )
    return _phone_from_value(candidate) or _raw_id(candidate)


def chat_name_for_message(message):
    payload = _payload(message)
    key = chat_key_for_message(message)
    return (
        _raw_id(payload.get("contactName"))
        or _raw_id(payload.get("chatName"))
        or (message.lead.name if message.lead else "")
        or _phone_from_value(payload.get("peerPhone"))
        or key
    )


def chat_phone_for_message(message):
    payload = _payload(message)
    return (
        _phone_from_value(payload.get("peerPhone"))
        or _phone_from_value(chat_key_for_message(message))
    )


def _canonical_peer(message, payload):
    is_group = bool(payload.get("isGroup"))
    raw_chat_id = _raw_id(
        payload.get("rawChatId") or payload.get("chatId")
    )

    if is_group:
        key = _raw_id(payload.get("peerKey")) or raw_chat_id
        return key, "", raw_chat_id or key

    phone = _phone_from_value(payload.get("peerPhone"))
    peer_key = _raw_id(payload.get("peerKey"))
    if not phone and peer_key and not peer_key.endswith("@lid"):
        phone = _phone_from_value(peer_key)

    # Older gateway payloads used from/to directly.  Only accept c.us/plain
    # phone ids here; an @lid numeric value is not a telephone number.
    if not phone:
        candidate = (
            payload.get("to")
            if bool(payload.get("fromMe"))
            else payload.get("from")
        )
        phone = _phone_from_value(candidate)

    key = phone or peer_key or raw_chat_id
    return key, phone, raw_chat_id


def repair_gateway_message_identity(*, message, payload, historical=False):
    """Enrich a persisted message with canonical phone/chat identity.

    History re-syncs are intentionally allowed to repair rows that already
    exist.  This is important for messages first imported while WhatsApp Web
    exposed the peer only as an @lid id.
    """
    if not message or not isinstance(payload, dict):
        return message

    key, peer_phone, raw_chat_id = _canonical_peer(message, payload)
    is_group = bool(payload.get("isGroup"))
    is_outbound = bool(payload.get("fromMe"))

    merged = dict(_payload(message))
    merged.update({k: v for k, v in payload.items() if v is not None})
    if key:
        merged["peerKey"] = key
    if peer_phone:
        merged["peerPhone"] = peer_phone
    if raw_chat_id:
        merged["rawChatId"] = raw_chat_id
    if historical or merged.get("isHistory"):
        merged["isHistory"] = True

    account_phone = normalize_whatsapp_number(
        phone_number=(
            message.account.display_phone_number
            or message.account.phone_number_id
        )
    )

    update_fields = []
    if merged != _payload(message):
        message.raw_payload = merged
        update_fields.append("raw_payload")

    if is_group:
        peer = key or raw_chat_id
    else:
        peer = peer_phone or key

    if peer:
        desired_from = account_phone if is_outbound else peer
        desired_to = peer if is_outbound else account_phone
        if desired_from and message.from_number != desired_from:
            message.from_number = desired_from
            update_fields.append("from_number")
        if desired_to and message.to_number != desired_to:
            message.to_number = desired_to
            update_fields.append("to_number")

    if not is_group and peer_phone:
        lead = Lead.objects.filter(
            organization=message.organization,
            phone=peer_phone,
        ).first()
        if lead and message.lead_id != lead.id:
            message.lead = lead
            update_fields.append("lead")

    if update_fields:
        update_fields.append("updated_at")
        message.save(update_fields=list(dict.fromkeys(update_fields)))

    return message


def _group_name(account_id):
    return f"hosted_whatsapp_{account_id}"


def broadcast_hosted_chat_refresh(*, account_id, reason="message", chat_key=""):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        _group_name(account_id),
        {
            "type": "hosted.refresh",
            "reason": reason,
            "chat_key": chat_key or "",
        },
    )


def queue_hosted_chat_refresh(*, account_id, reason="message", chat_key=""):
    transaction.on_commit(
        lambda: broadcast_hosted_chat_refresh(
            account_id=account_id,
            reason=reason,
            chat_key=chat_key,
        )
    )


@transaction.atomic
def handle_hosted_gateway_event(*, payload):
    """Run canonical hosted event handling plus identity repair/broadcast."""
    event = str(payload.get("event") or "").strip().lower()
    result = legacy_handle_gateway_event(payload=payload)

    if event == "history_sync":
        last_key = ""
        for item in payload.get("messages") or []:
            if not isinstance(item, dict) or not item.get("messageId"):
                continue
            message = (
                WhatsAppMessage.objects.select_related("account", "lead")
                .filter(external_id=f"wweb:{item['messageId']}")
                .first()
            )
            if message:
                repair_gateway_message_identity(
                    message=message,
                    payload=item,
                    historical=True,
                )
                last_key = chat_key_for_message(message) or last_key
        if result is not None:
            account_id = getattr(result, "account_id", None) or getattr(result, "id", None)
            # legacy history handler returns a message when at least one was
            # persisted, otherwise the account itself.
            if isinstance(result, WhatsAppMessage):
                account_id = result.account_id
            if account_id:
                queue_hosted_chat_refresh(
                    account_id=account_id,
                    reason="history",
                    chat_key=last_key,
                )
        return result

    if event == "message" and isinstance(result, WhatsAppMessage):
        repair_gateway_message_identity(
            message=result,
            payload=payload,
            historical=False,
        )
        queue_hosted_chat_refresh(
            account_id=result.account_id,
            reason="message",
            chat_key=chat_key_for_message(result),
        )
        return result

    if event == "message_ack" and isinstance(result, WhatsAppMessage):
        queue_hosted_chat_refresh(
            account_id=result.account_id,
            reason="status",
            chat_key=chat_key_for_message(result),
        )
        return result

    if event in {"history_complete", "history_failed"}:
        session_id = payload.get("sessionId")
        if session_id:
            queue_hosted_chat_refresh(
                account_id=session_id,
                reason=event,
            )

    return result


def _search_matches(row, query):
    query = str(query or "").strip().lower()
    if not query:
        return True

    digits_query = "".join(ch for ch in query if ch.isdigit())
    haystack = " ".join(
        str(row.get(field) or "")
        for field in ("name", "key", "phone", "raw_chat_id")
    ).lower()
    if query in haystack:
        return True
    if digits_query:
        haystack_digits = "".join(ch for ch in haystack if ch.isdigit())
        return digits_query in haystack_digits
    return False


def build_hosted_chat_snapshot(*, account, selected_chat="", query=""):
    """Build a WhatsApp-like conversation list and complete selected thread."""
    recent_messages = list(
        WhatsAppMessage.objects.filter(
            organization=account.organization,
            account=account,
        )
        .select_related("lead", "account")
        .order_by("-created_at")[:MAX_CONVERSATION_SCAN]
    )

    conversations = {}
    for message in recent_messages:
        key = chat_key_for_message(message)
        if not key:
            continue
        if key not in conversations:
            payload = _payload(message)
            conversations[key] = {
                "key": key,
                "name": chat_name_for_message(message),
                "phone": chat_phone_for_message(message),
                "raw_chat_id": _raw_id(
                    payload.get("rawChatId") or payload.get("chatId")
                ),
                "is_group": bool(payload.get("isGroup")),
                "last_message": (
                    message.body or message.get_message_type_display()
                ),
                "last_at": message.created_at,
                "unread": 0,
            }
        if (
            message.direction == WhatsAppMessage.Direction.INBOUND
            and not message.is_read
        ):
            conversations[key]["unread"] += 1

    rows = list(conversations.values())
    rows = [row for row in rows if _search_matches(row, query)]
    rows = rows[:MAX_CONVERSATIONS]

    selected = str(selected_chat or "").strip()
    if not selected and rows:
        selected = rows[0]["key"]

    # The conversation list is bounded for speed, but the selected thread is
    # independently scanned so a busy inbox cannot truncate the open chat to
    # whichever messages happened to fit in the global list window.
    thread = []
    if selected:
        thread_scan = list(
            WhatsAppMessage.objects.filter(
                organization=account.organization,
                account=account,
            )
            .select_related("lead", "account")
            .order_by("-created_at")[:MAX_CONVERSATION_SCAN]
        )
        thread = [
            message
            for message in reversed(thread_scan)
            if chat_key_for_message(message) == selected
        ][-MAX_THREAD_MESSAGES:]

    selected_row = conversations.get(selected, {})
    selected_name = selected_row.get("name") or selected

    return {
        "conversations": rows,
        "selected_chat": selected,
        "selected_name": selected_name,
        "thread": thread,
        "total_conversations": len(conversations),
    }


def serialize_hosted_chat_snapshot(snapshot):
    return {
        "conversations": [
            {
                **row,
                "last_at": row["last_at"].isoformat(),
            }
            for row in snapshot["conversations"]
        ],
        "selected_chat": snapshot["selected_chat"],
        "selected_name": snapshot["selected_name"],
        "total_conversations": snapshot["total_conversations"],
        "thread": [
            {
                "id": str(message.id),
                "direction": message.direction,
                "body": message.body,
                "message_type": message.message_type,
                "message_type_label": message.get_message_type_display(),
                "status": message.status,
                "status_label": message.get_status_display(),
                "created_at": message.created_at.isoformat(),
            }
            for message in snapshot["thread"]
        ],
    }
