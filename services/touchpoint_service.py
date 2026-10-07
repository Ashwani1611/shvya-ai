"""Tenant-scoped Touchpoint authoring and lead-time personalization.

Messages stay as templates in the library. Resolve CRM values only for the
current lead, and never expose private file-storage URLs to the browser.
"""
import mimetypes
import re
from pathlib import Path

from django.core.exceptions import ValidationError

from apps.followups.touchpoint_models import TouchpointAttachment
from services.channels.template_service import available_placeholders


MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
ATTACHMENT_EXTENSIONS = {
    "image": frozenset({".jpg", ".jpeg", ".png"}),
    "video": frozenset({".mp4", ".mov", ".webm", ".3gp"}),
    "document": frozenset({
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".ppt", ".pptx", ".txt", ".csv",
    }),
}
TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]{0,63})\s*\}\}")


def placeholder_keys(organization):
    return {item["key"] for item in available_placeholders(organization=organization)}


def validate_reply_placeholders(*, organization, body):
    keys = placeholder_keys(organization)
    invalid = sorted({m.group(1) for m in TOKEN.finditer(body or "")} - keys)
    if "{{" in TOKEN.sub("", body or "") or "}}" in TOKEN.sub("", body or ""):
        raise ValidationError("Malformed CRM placeholder. Insert a valid field from the picker.")
    if invalid:
        raise ValidationError(
            "Unknown CRM placeholder(s): " + ", ".join(invalid) +
            ". Select a field from the personalization options."
        )


def render_touchpoint(*, reply, lead, user=None, allowed_keys=None, values=None):
    """Return a per-lead preview plus the missing values, never raw HTML."""
    # Use the same lookup map as Sequence and Meta CRM templates.
    from services.followup_service import _lead_template_values

    keys = allowed_keys if allowed_keys is not None else placeholder_keys(lead.organization)
    values = values if values is not None else _lead_template_values(lead, user)
    missing = set()

    def substitute(match):
        key = match.group(1)
        if key not in keys:
            missing.add(key)
            return ""
        value = values.get(key)
        if value in (None, ""):
            missing.add(key)
            return ""
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        if isinstance(value, dict):
            missing.add(key)
            return ""
        return str(value)

    return TOKEN.sub(substitute, reply.body), sorted(missing)


def attachment_kind(filename):
    ext = Path(filename or "").suffix.lower()
    for kind, extensions in ATTACHMENT_EXTENSIONS.items():
        if ext in extensions:
            return kind
    raise ValidationError("Unsupported file. Upload a photo, video, PDF, Office file, TXT or CSV.")


def validate_attachment_changes(*, reply, uploads, remove_ids):
    """Validate the complete resulting library entry before any files are saved."""
    uploaded = list(uploads or [])
    existing = list(reply.attachments.all()) if not reply._state.adding else []
    existing_by_id = {str(item.pk): item for item in existing}
    requested = set(str(value) for value in (remove_ids or []))
    if requested - existing_by_id.keys():
        raise ValidationError("An attachment does not belong to this Touchpoint.")

    remaining = [item for item in existing if str(item.pk) not in requested]
    if len(remaining) + len(uploaded) > MAX_ATTACHMENTS:
        raise ValidationError(f"Use up to {MAX_ATTACHMENTS} attachments per Touchpoint.")
    total = sum(item.size for item in remaining)
    for upload in uploaded:
        filename = Path(getattr(upload, "name", "") or "").name
        if not filename or len(filename) > 255:
            raise ValidationError("Attachment filenames must be 1-255 characters.")
        kind = attachment_kind(filename)
        size = int(getattr(upload, "size", 0) or 0)
        if not 0 < size <= MAX_ATTACHMENT_BYTES:
            raise ValidationError("Each attachment must be nonempty and at most 25 MB.")
        submitted_mime = str(getattr(upload, "content_type", "") or "").lower().split(";", 1)[0]
        if kind in {"image", "video"} and submitted_mime and not submitted_mime.startswith(kind + "/"):
            raise ValidationError("The uploaded media type does not match the file extension.")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise ValidationError("Touchpoint attachments may total at most 50 MB.")
    return uploaded, requested


def apply_attachment_changes(*, reply, uploads, remove_ids):
    """Called inside the same transaction as the saved reply."""
    if remove_ids:
        reply.attachments.filter(id__in=remove_ids).delete()
    next_position = max(
        reply.attachments.values_list("position", flat=True), default=0
    ) + 1
    for upload in uploads:
        filename = Path(upload.name).name
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if filename.lower().endswith(".csv"):
            mime_type = "text/plain"  # Meta requires text/plain for CSV documents.
        TouchpointAttachment.objects.create(
            reply=reply, file=upload, original_name=filename,
            mime_type=mime_type, size=upload.size, position=next_position,
        )
        next_position += 1
