"""Small, read-only projection of Meta image/document webhook metadata."""

import mimetypes
import re

from .models import WhatsAppMessage


_MEDIA_ID = re.compile(r"[0-9]{1,64}\Z", re.ASCII)
_MIME_TYPE = re.compile(r"[a-z0-9.+-]+/[a-z0-9.+-]+\Z", re.ASCII)


def meta_inbound_media(raw_payload):
    """Return a model type and allowlisted metadata, never a provider URL.

    Use the stored webhook for both fresh events and historical render-only
    recovery. A download is bound to this message's received media ID.
    """
    if not isinstance(raw_payload, dict):
        return WhatsAppMessage.MessageType.TEXT, {}
    media_type = raw_payload.get("type")
    if media_type not in ("image", "document"):
        return WhatsAppMessage.MessageType.TEXT, {}
    raw_media = raw_payload.get(media_type)
    if not isinstance(raw_media, dict):
        return media_type, {}
    payload = {"source": "meta_inbound"}
    media_id = raw_media.get("id")
    if isinstance(media_id, str) and _MEDIA_ID.fullmatch(media_id):
        payload["media_id"] = media_id
    mime_type = raw_media.get("mime_type")
    if isinstance(mime_type, str) and len(mime_type) <= 120:
        mime_type = mime_type.lower()
        if _MIME_TYPE.fullmatch(mime_type):
            payload["mime_type"] = mime_type
    filename = raw_media.get("filename")
    if isinstance(filename, str) and len(filename) <= 4096:
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(char for char in filename if char.isprintable())[:200].strip()
        if filename:
            payload["filename"] = filename
    return media_type, payload


def attachment_filename(media_type, payload):
    filename = payload.get("filename")
    if filename:
        return filename
    extension = mimetypes.guess_extension(payload.get("mime_type", "")) or ""
    return f"WhatsApp {media_type}{extension}"
