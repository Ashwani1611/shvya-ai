"""Hosted WhatsApp chat read-model, identity repair, and realtime notifications.

The linked-device gateway is the source of WhatsApp chat identity. Current
WhatsApp Web can expose direct chats as ``@lid`` ids, so a LID must never be
mistaken for a phone number. The gateway sends ``peerKey`` / ``peerPhone``
metadata and this module keeps persisted WhatsAppMessage rows aligned with
that canonical identity.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Q

from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead
from services.channels.hosted_whatsapp_service import (
    handle_gateway_event as legacy_handle_gateway_event,
    normalize_whatsapp_number,
)


MAX_CONVERSATION_SCAN = 20000
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


def _raw_chat_id_for_message(message):
    payload = _payload(message)
    return _raw_id(payload.get("rawChatId") or payload.get("chatId"))


def _is_lid_derived_phone(phone, raw_chat_id):
    """Reject pseudo-phones created by stripping ``@lid`` from a chat id."""
    phone = _phone_from_value(phone)
    raw_chat_id = _raw_id(raw_chat_id)
    if not phone or not raw_chat_id.endswith("@lid"):
        return False
    lid_digits = "".join(ch for ch in raw_chat_id.split("@", 1)[0] if ch.isdigit())
    phone_digits = "".join(ch for ch in phone if ch.isdigit())
    return bool(lid_digits and phone_digits and lid_digits == phone_digits)


def _valid_peer_phone(value, raw_chat_id):
    phone = _phone_from_value(value)
    if _is_lid_derived_phone(phone, raw_chat_id):
        return ""
    return phone


def _message_sort_key(message):
    """Use WhatsApp's event timestamp for ordering, falling back to DB time."""
    try:
        timestamp = float(_payload(message).get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        timestamp = message.created_at.timestamp()
    return timestamp, str(message.id)


def chat_key_for_message(message):
    """Canonical conversation key carried directly on one message."""
    payload = _payload(message)
    raw_chat_id = _raw_chat_id_for_message(message)

    peer_key = _raw_id(payload.get("peerKey"))
    if peer_key:
        if peer_key.endswith("@lid"):
            peer_phone = _valid_peer_phone(payload.get("peerPhone"), raw_chat_id)
            return peer_phone or peer_key
        peer_key_phone = _valid_peer_phone(peer_key, raw_chat_id)
        if peer_key_phone:
            return peer_key_phone
        if "@" in peer_key:
            return peer_key

    peer_phone = _valid_peer_phone(payload.get("peerPhone"), raw_chat_id)
    if peer_phone:
        return peer_phone

    if payload.get("isGroup"):
        return _raw_id(payload.get("chatId")) or message.from_number

    candidate = (
        message.from_number
        if message.direction == WhatsAppMessage.Direction.INBOUND
        else message.to_number
    )
    candidate_phone = _valid_peer_phone(candidate, raw_chat_id)
    return candidate_phone or raw_chat_id or _raw_id(candidate)


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
    raw_chat_id = _raw_chat_id_for_message(message)
    return (
        _valid_peer_phone(payload.get("peerPhone"), raw_chat_id)
        or _valid_peer_phone(chat_key_for_message(message), raw_chat_id)
    )


def _canonical_peer(message, payload):
    is_group = bool(payload.get("isGroup"))
    raw_chat_id = _raw_id(payload.get("rawChatId") or payload.get("chatId"))

    if is_group:
        key = _raw_id(payload.get("peerKey")) or raw_chat_id
        return key, "", raw_chat_id or key

    phone = _valid_peer_phone(payload.get("peerPhone"), raw_chat_id)
    peer_key = _raw_id(payload.get("peerKey"))
    if not phone and peer_key and not peer_key.endswith("@lid"):
        phone = _valid_peer_phone(peer_key, raw_chat_id)

    if not phone:
        candidate = payload.get("to") if bool(payload.get("fromMe")) else payload.get("from")
        phone = _valid_peer_phone(candidate, raw_chat_id)

    if not phone:
        existing_payload = _payload(message)
        existing_raw_chat_id = (
            raw_chat_id
            or _raw_id(existing_payload.get("rawChatId") or existing_payload.get("chatId"))
        )
        phone = _valid_peer_phone(existing_payload.get("peerPhone"), existing_raw_chat_id)

    key = phone or (peer_key if peer_key.endswith("@lid") else "") or raw_chat_id
    return key, phone, raw_chat_id


def repair_gateway_message_identity(*, message, payload, historical=False):
    """Enrich a persisted message with canonical phone/chat identity.

    History re-syncs are intentionally allowed to repair rows that already
    exist. This repairs messages first imported while WhatsApp exposed only a
    LID instead of the contact's phone number.
    """
    if not message or not isinstance(payload, dict):
        return message

    key, peer_phone, raw_chat_id = _canonical_peer(message, payload)
    is_group = bool(payload.get("isGroup"))
    is_outbound = bool(payload.get("fromMe"))

    merged = dict(_payload(message))
    incoming = {k: v for k, v in payload.items() if v is not None}
    if _is_lid_derived_phone(incoming.get("peerPhone"), raw_chat_id):
        incoming.pop("peerPhone", None)
    if _is_lid_derived_phone(incoming.get("peerKey"), raw_chat_id):
        incoming.pop("peerKey", None)
    merged.update(incoming)
    if key:
        merged["peerKey"] = key
    if peer_phone:
        merged["peerPhone"] = peer_phone
    if raw_chat_id:
        merged["rawChatId"] = raw_chat_id
    if historical or merged.get("isHistory"):
        merged["isHistory"] = True

    account_phone = normalize_whatsapp_number(
        phone_number=(message.account.display_phone_number or message.account.phone_number_id)
    )

    update_fields = []
    if merged != _payload(message):
        message.raw_payload = merged
        update_fields.append("raw_payload")

    peer = (key or raw_chat_id) if is_group else (peer_phone or key)
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
        account_id = None
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
                account_id = message.account_id
                last_key = chat_key_for_message(message) or last_key
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

    if event == "message_ack":
        session_id = payload.get("sessionId")
        if session_id:
            queue_hosted_chat_refresh(account_id=session_id, reason="status")
        return result

    if event in {"history_complete", "history_failed"}:
        session_id = payload.get("sessionId")
        if session_id:
            queue_hosted_chat_refresh(account_id=session_id, reason=event)

    return result


def _conversation_aliases(messages):
    """Map every raw WhatsApp chat id to its resolved canonical phone/key.

    Older rows may have been saved while WhatsApp exposed only ``@lid``. A
    later message from the same raw chat can carry the real phone. Applying
    this map while reading keeps those rows in one conversation immediately,
    even before every old database row has been physically repaired.
    """
    aliases = {}
    for message in messages:
        raw_chat_id = _raw_chat_id_for_message(message)
        if not raw_chat_id:
            continue
        payload = _payload(message)
        if payload.get("isGroup"):
            aliases.setdefault(raw_chat_id, raw_chat_id)
            continue
        phone = chat_phone_for_message(message)
        if phone:
            # Messages are sorted newest-first. Keep the first trustworthy
            # resolution and never let an older legacy row overwrite it.
            aliases.setdefault(raw_chat_id, phone)
    return aliases


def _canonical_chat_key(message, aliases):
    raw_chat_id = _raw_chat_id_for_message(message)
    if raw_chat_id and raw_chat_id in aliases:
        return aliases[raw_chat_id]
    key = chat_key_for_message(message)
    return aliases.get(key, key)


def _name_quality(value, *, key="", phone="", raw_chat_id=""):
    value = _raw_id(value)
    if not value:
        return 0
    if value in {key, phone, raw_chat_id}:
        return 1
    if value.endswith(("@lid", "@c.us", "@g.us")):
        return 1
    if value.lstrip("+").isdigit():
        return 1
    return 3


def _search_matches(row, query):
    query = str(query or "").strip().lower()
    if not query:
        return True

    raw_ids = row.get("raw_chat_ids") or []
    haystack = " ".join(
        [
            str(row.get("name") or ""),
            str(row.get("key") or ""),
            str(row.get("phone") or ""),
            str(row.get("raw_chat_id") or ""),
            *(str(value or "") for value in raw_ids),
        ]
    ).lower()
    if query in haystack:
        return True

    digits_query = "".join(ch for ch in query if ch.isdigit())
    if digits_query:
        haystack_digits = "".join(ch for ch in haystack if ch.isdigit())
        return digits_query in haystack_digits
    return False


def _selected_thread_queryset(account, selected, raw_chat_ids=None):
    base = WhatsAppMessage.objects.filter(
        organization=account.organization,
        account=account,
    ).select_related("lead", "account")

    raw_chat_ids = [value for value in (raw_chat_ids or []) if value]

    if selected.startswith("+"):
        lookup = (
            Q(from_number=selected)
            | Q(to_number=selected)
            | Q(raw_payload__peerPhone=selected)
            | Q(raw_payload__peerKey=selected)
        )
        if raw_chat_ids:
            lookup |= Q(raw_payload__rawChatId__in=raw_chat_ids)
            lookup |= Q(raw_payload__chatId__in=raw_chat_ids)
        return base.filter(lookup)

    if "@" in selected:
        return base.filter(
            Q(from_number=selected)
            | Q(to_number=selected)
            | Q(raw_payload__peerKey=selected)
            | Q(raw_payload__rawChatId=selected)
            | Q(raw_payload__chatId=selected)
        )

    return base


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
    recent_messages.sort(key=_message_sort_key, reverse=True)

    aliases = _conversation_aliases(recent_messages)
    conversations = {}

    for message in recent_messages:
        key = _canonical_chat_key(message, aliases)
        if not key:
            continue

        payload = _payload(message)
        raw_chat_id = _raw_chat_id_for_message(message)
        phone = chat_phone_for_message(message)
        if not phone and raw_chat_id:
            phone = _phone_from_value(aliases.get(raw_chat_id))
        name = chat_name_for_message(message)

        if key not in conversations:
            conversations[key] = {
                "key": key,
                "name": name,
                "phone": phone,
                "raw_chat_id": raw_chat_id,
                "raw_chat_ids": [raw_chat_id] if raw_chat_id else [],
                "is_group": bool(payload.get("isGroup")),
                "last_message": message.body or message.get_message_type_display(),
                "last_at": message.created_at,
                "unread": 0,
            }
        else:
            row = conversations[key]
            if raw_chat_id and raw_chat_id not in row["raw_chat_ids"]:
                row["raw_chat_ids"].append(raw_chat_id)
            if not row["phone"] and phone:
                row["phone"] = phone
            if not row["raw_chat_id"] and raw_chat_id:
                row["raw_chat_id"] = raw_chat_id

            current_quality = _name_quality(
                row["name"],
                key=key,
                phone=row["phone"],
                raw_chat_id=row["raw_chat_id"],
            )
            candidate_quality = _name_quality(
                name,
                key=key,
                phone=phone,
                raw_chat_id=raw_chat_id,
            )
            if candidate_quality > current_quality:
                row["name"] = name

        if message.direction == WhatsAppMessage.Direction.INBOUND and not message.is_read:
            conversations[key]["unread"] += 1

    rows = [row for row in conversations.values() if _search_matches(row, query)]
    rows = rows[:MAX_CONVERSATIONS]

    selected = str(selected_chat or "").strip()
    selected = aliases.get(selected, selected)
    if not selected and rows:
        selected = rows[0]["key"]

    thread = []
    if selected:
        selected_raw_ids = [
            raw_id
            for raw_id, canonical in aliases.items()
            if canonical == selected
        ]
        candidates = list(
            _selected_thread_queryset(
                account,
                selected,
                raw_chat_ids=selected_raw_ids,
            )
            .order_by("-created_at")[:MAX_THREAD_MESSAGES]
        )
        thread = [
            message
            for message in candidates
            if _canonical_chat_key(message, aliases) == selected
        ]
        thread.sort(key=_message_sort_key)

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
            {**row, "last_at": row["last_at"].isoformat()}
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
