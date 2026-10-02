"""Committed, tenant-scoped Hosted message deltas for the live inbox.

This publishes UI state only: it never calls a provider, changes message status,
creates AI work, or treats a queued message as sent. Snapshot polling remains
an independent recovery path for dropped notifications and bulk SQL updates.
"""

import asyncio
import logging
from pathlib import Path

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.urls import reverse

from apps.channels.models import WhatsAppMessage
from services.channels.hosted_chat_service import (
    chat_key_for_message,
    chat_name_for_message,
    chat_phone_for_message,
)
from services.channels.hosted_message_content import display_body_for_message, display_text_for_message

logger = logging.getLogger(__name__)


def message_event(message):
    """Only allowlisted display data crosses the socket; never raw_payload."""
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    key = chat_key_for_message(message)
    aliases = list(dict.fromkeys(str(value) for value in (
        key, raw.get("rawChatId"), raw.get("chatId"), raw.get("peerKey"),
    ) if value))
    if raw.get("isHistory") is True or raw.get("isStatus") or any(
        value == "status@broadcast" or "@newsletter" in value for value in aliases
    ):
        return None
    automated = "shvya_ai" in raw or "shvya_welcome" in raw
    cancelled = (
        automated and message.direction == "outbound" and message.status == "failed"
        and not message.sent_at and not message.external_id
        and str(message.error or "").lower().startswith(("ai send cancelled", "superseded"))
    )
    body = display_body_for_message(message)
    if not body and message.message_type == "text":
        body = display_text_for_message(message)
    event = {
        "type": "hosted.message", "account_id": str(message.account_id),
        "organization_id": str(message.organization_id),
        "chat_key": key, "aliases": aliases, "message_id": str(message.pk),
        "updated_at": message.updated_at.isoformat(),
        "operation": "remove" if cancelled else "upsert",
    }
    if cancelled:
        return event
    item = {
        "id": str(message.pk), "direction": message.direction, "body": body,
        "message_type": message.message_type,
        "message_type_label": message.get_message_type_display(),
        "status": message.status, "status_label": message.get_status_display(),
        "created_at": message.created_at.isoformat(),
        "updated_at": message.updated_at.isoformat(), "is_read": message.is_read,
    }
    if message.message_type != "text":
        media = message.media_payload if isinstance(message.media_payload, dict) else {}
        url = reverse("whatsapp-hosted-session-chat-media", args=[message.account_id, message.pk])
        item.update(media_url=url, media_download_url=f"{url}?download=1", filename=Path(str(
            media.get("filename") or raw.get("filename") or raw.get("fileName") or "Attachment"
        )).name[:240])
    lead = message.lead
    event["message"] = item
    event["conversation"] = {
        "key": key, "name": chat_name_for_message(message), "phone": chat_phone_for_message(message),
        "raw_chat_ids": aliases, "raw_chat_id": str(raw.get("rawChatId") or raw.get("chatId") or ""),
        "is_group": bool(raw.get("isGroup")),
        "last_at": message.created_at.isoformat(), "last_message_id": str(message.pk),
        "last_message": body or message.get_message_type_display(),
        "lead_id": str(lead.pk) if lead else "",
        "stage_name": lead.stage.name if lead and lead.stage_id else "",
    }
    return event


async def _publish(layer, group, event):
    # UI signalling cannot hold a provider worker indefinitely if Redis stalls.
    await asyncio.wait_for(layer.group_send(group, event), timeout=2)


def broadcast_committed_message(message_id, using="default"):
    try:
        message = WhatsAppMessage.objects.using(using).select_related(
            "account", "lead", "lead__stage",
        ).filter(pk=message_id, account__connection_type="hosted", account__is_active=True).first()
        if message is None or message.account.organization_id != message.organization_id:
            return
        if message.lead and message.lead.organization_id != message.organization_id:
            return
        event = message_event(message)
        layer = get_channel_layer()
        if event is not None and layer is not None:
            async_to_sync(_publish)(layer, f"hosted_whatsapp_{message.account_id}", event)
    except Exception:
        # The database row is already committed. Do not fail or retry the send
        # because its optional UI notification could not be delivered.
        logger.exception("Hosted inbox notification failed for message %s", message_id)
