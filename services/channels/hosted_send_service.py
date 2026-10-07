"""Queue Hosted Account outbound text/media without mixing Meta API transport."""

import logging
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import get_valid_filename

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    normalize_whatsapp_number,
)


MAX_HOSTED_UPLOAD_BYTES = 25 * 1024 * 1024
logger = logging.getLogger(__name__)

_ALLOWED_EXTENSIONS = {
    WhatsAppMessage.MessageType.IMAGE: {".jpg", ".jpeg", ".png", ".webp", ".gif"},
    WhatsAppMessage.MessageType.VIDEO: {".mp4", ".3gp", ".mov", ".m4v", ".webm"},
    WhatsAppMessage.MessageType.DOCUMENT: {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv",
    },
}


def expire_abandoned_hosted_manual_sends(*, now=None, limit=100):
    """End abandoned in-flight UI states without repeating a provider send.

    Ten minutes exceeds the upload timeout and the acknowledgement deadline.
    A later authenticated receipt can still confirm the original message.
    """
    cutoff = (now or timezone.now()) - timedelta(minutes=10)
    candidates = WhatsAppMessage.objects.filter(
        account__connection_type=WhatsAppAccount.ConnectionType.coexisted,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status="sending",
        updated_at__lte=cutoff,
        raw_payload__shvya_hosted__origin="agent",
    ).exclude(
        Q(raw_payload__has_key="shvya_ai")
        | Q(raw_payload__has_key="shvya_welcome")
        | Q(raw_payload__has_key="shvya_auto_followup")
        | Q(raw_payload__has_key="shvya_workflow")
        | Q(raw_payload__has_key="shvya_sales")
    )
    identifiers = list(candidates.order_by("updated_at", "pk").values_list(
        "pk", "organization_id", "account_id",
    )[:max(1, min(int(limit), 100))])
    expired = 0
    for message_id, organization_id, account_id in identifiers:
        with transaction.atomic():
            message = candidates.select_for_update(skip_locked=True).filter(
                pk=message_id, organization_id=organization_id, account_id=account_id,
            ).first()
            if message is None:
                continue
            message.status = WhatsAppMessage.Status.FAILED
            message.error = (
                "Delivery is unconfirmed after the send process was interrupted. "
                "Check the conversation before sending this message again."
            )
            message.save(update_fields=["status", "error", "updated_at"])
        expired += 1
        try:
            from services.channels.hosted_chat_service import chat_key_for_message, queue_hosted_chat_refresh

            queue_hosted_chat_refresh(
                account_id=message.account_id, reason="failed", chat_key=chat_key_for_message(message),
            )
        except Exception:
            logger.exception("Could not publish interrupted Hosted send for %s", message.pk)
    return expired


def _normalize_recipient(value):
    raw = str(value or "").strip()
    if raw.endswith("@g.us") or raw.endswith("@lid"):
        return raw
    normalized = normalize_whatsapp_number(phone_number=raw)
    if not normalized:
        raise HostedWhatsAppValidationError("Invalid WhatsApp recipient.")
    return normalized


def _validate_upload(uploaded_file, message_type):
    if message_type not in _ALLOWED_EXTENSIONS:
        raise HostedWhatsAppValidationError("Unsupported attachment type.")
    if not uploaded_file:
        raise HostedWhatsAppValidationError("Choose a file to send.")
    if uploaded_file.size <= 0:
        raise HostedWhatsAppValidationError("The selected file is empty.")
    if uploaded_file.size > MAX_HOSTED_UPLOAD_BYTES:
        raise HostedWhatsAppValidationError("Attachments must be 25 MB or smaller.")

    original_name = Path(uploaded_file.name or "attachment").name
    extension = Path(original_name).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS[message_type]:
        raise HostedWhatsAppValidationError(
            f"Unsupported {message_type} file format: {extension or 'unknown'}"
        )

    mime_type = str(getattr(uploaded_file, "content_type", "") or "").split(";", 1)[0]
    if message_type == WhatsAppMessage.MessageType.IMAGE and not mime_type.startswith("image/"):
        raise HostedWhatsAppValidationError("Selected photo is not a valid image file.")
    if message_type == WhatsAppMessage.MessageType.VIDEO and not mime_type.startswith("video/"):
        raise HostedWhatsAppValidationError("Selected video is not a valid video file.")
    if not mime_type:
        mime_type = "application/octet-stream"

    return get_valid_filename(original_name) or f"attachment{extension}", mime_type


def queue_hosted_uploaded_media(
    *,
    account,
    to_number,
    uploaded_file,
    message_type,
    caption="",
    lead=None,
):
    if account.connection_type != WhatsAppAccount.ConnectionType.coexisted:
        raise HostedWhatsAppValidationError("This is not a Hosted Account session.")

    recipient = _normalize_recipient(to_number)
    filename, mime_type = _validate_upload(uploaded_file, message_type)
    storage_path = (
        f"hosted_whatsapp/outbound/{account.organization_id}/{account.id}/"
        f"{uuid4().hex}-{filename}"
    )
    saved_path = default_storage.save(storage_path, uploaded_file)

    try:
        return WhatsAppMessage.objects.create(
            organization=account.organization,
            account=account,
            lead=lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=account.display_phone_number or account.phone_number_id,
            to_number=recipient,
            body=str(caption or "").strip(),
            message_type=message_type,
            media_payload={
                "source": "storage",
                "storage_path": saved_path,
                "filename": filename,
                "mime_type": mime_type,
            },
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload={
                "shvya_hosted": {
                    "origin": "agent",
                    "chat_id": str(to_number or ""),
                    "attachment": True,
                }
            },
        )
    except Exception:
        default_storage.delete(saved_path)
        raise
