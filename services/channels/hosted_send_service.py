"""Queue Hosted Account outbound text/media without mixing Meta API transport."""

from pathlib import Path
from uuid import uuid4

from django.core.files.storage import default_storage
from django.utils.text import get_valid_filename

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    normalize_whatsapp_number,
)


MAX_HOSTED_UPLOAD_BYTES = 25 * 1024 * 1024

_ALLOWED_EXTENSIONS = {
    WhatsAppMessage.MessageType.IMAGE: {".jpg", ".jpeg", ".png", ".webp", ".gif"},
    WhatsAppMessage.MessageType.VIDEO: {".mp4", ".3gp", ".mov", ".m4v", ".webm"},
    WhatsAppMessage.MessageType.DOCUMENT: {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv",
    },
}


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
