"""Safe attachment payload decoding for Operations MCP Cadence tools."""

from __future__ import annotations

import base64
import binascii
import hashlib

from django.core.files.base import ContentFile

from services.followup_service import (
    FollowupError,
    MAX_EMAIL_ATTACHMENT_BYTES,
    MAX_EMAIL_ATTACHMENTS,
    validate_email_attachments,
)


class AttachmentPayloadError(ValueError):
    pass


def redact_attachment_content(data):
    safe = dict(data or {})
    items = safe.get("attachments")
    if isinstance(items, list):
        redacted = []
        for item in items:
            if isinstance(item, dict):
                clean = dict(item)
                if "content_base64" in clean:
                    clean["content_base64"] = "<binary omitted>"
                redacted.append(clean)
            else:
                redacted.append(item)
        safe["attachments"] = redacted
    if "attachment_base64" in safe:
        safe["attachment_base64"] = "<binary omitted>"
    return safe


def decode_email_attachments(data):
    items = (data or {}).get("attachments")
    if items is None:
        return None, []
    if not isinstance(items, list):
        raise AttachmentPayloadError("attachments must be an array.")
    if len(items) > MAX_EMAIL_ATTACHMENTS:
        raise AttachmentPayloadError(
            f"Email Cadence steps support up to {MAX_EMAIL_ATTACHMENTS} attachments."
        )

    uploads = []
    descriptors = []
    total = 0
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise AttachmentPayloadError(
                f"Email attachment {index} must be an object."
            )
        name = str(item.get("name") or "").strip()
        encoded = str(item.get("content_base64") or "").strip()
        mime_type = str(item.get("mime_type") or "").strip()
        if not name:
            raise AttachmentPayloadError(
                f"Email attachment {index} requires name."
            )
        if not encoded:
            raise AttachmentPayloadError(
                f"Email attachment {index} requires content_base64."
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AttachmentPayloadError(
                f"Email attachment {index} content_base64 is invalid."
            ) from exc
        if not raw:
            raise AttachmentPayloadError(
                f"Email attachment {index} cannot be empty."
            )
        total += len(raw)
        if total > MAX_EMAIL_ATTACHMENT_BYTES:
            raise AttachmentPayloadError(
                "Email Cadence attachments can total at most 18 MiB per step."
            )
        upload = ContentFile(raw, name=name)
        upload.content_type = mime_type
        uploads.append(upload)
        descriptors.append(
            {
                "name": name,
                "mime_type": mime_type,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    try:
        validate_email_attachments(uploads)
    except FollowupError as exc:
        raise AttachmentPayloadError(str(exc)) from exc
    return uploads, descriptors
