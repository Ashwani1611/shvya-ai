from __future__ import annotations

import hashlib
from datetime import datetime
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import get_valid_filename
from xhtml2pdf import pisa

from apps.sales.activity import record_activity


class SalesPDFError(RuntimeError):
    pass


def _local_asset_path(uri):
    """Resolve only SHVYA-owned static/media URLs; never server-fetch arbitrary URLs."""
    value = str(uri or "")
    if not value:
        return ""
    media_url = str(settings.MEDIA_URL or "")
    static_url = str(settings.STATIC_URL or "")
    if media_url and value.startswith(media_url):
        candidate = Path(settings.MEDIA_ROOT) / value[len(media_url):].lstrip("/")
        return str(candidate) if candidate.exists() else ""
    if static_url and value.startswith(static_url):
        relative = value[len(static_url):].lstrip("/")
        for root in getattr(settings, "STATICFILES_DIRS", []):
            candidate = Path(root) / relative
            if candidate.exists():
                return str(candidate)
        candidate = Path(getattr(settings, "STATIC_ROOT", "")) / relative
        return str(candidate) if candidate.exists() else ""
    return ""


def _link_callback(uri, rel):
    local = _local_asset_path(uri)
    if local:
        return local
    # User-supplied remote URLs are intentionally not fetched from the server.
    # This prevents PDF rendering from becoming an SSRF primitive.
    return ""


def _pdf_asset_url(snapshot, key):
    value = str((snapshot or {}).get(key) or "")
    return value if _local_asset_path(value) else ""


def _acceptance_recorded_display(document):
    value = document.signed_at or document.accepted_at
    if not value:
        return ""
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        zone = value.strftime("%Z")
        rendered = value.strftime("%d %b %Y, %H:%M")
        return f"{rendered} {zone}".strip()
    return value.strftime("%d %b %Y")


def build_pdf_bytes(document):
    snapshot = document.presentation_snapshot or {}
    html = render_to_string(
        "sales/pdf_document.html",
        {
            "document": document,
            "presentation": snapshot,
            "pdf_logo_src": _pdf_asset_url(snapshot, "logo_url"),
            "pdf_signature_src": _pdf_asset_url(snapshot, "signature_url"),
            "acceptance_recorded_display": _acceptance_recorded_display(document),
        },
    )
    output = BytesIO()
    result = pisa.CreatePDF(
        src=html,
        dest=output,
        encoding="utf-8",
        link_callback=_link_callback,
    )
    if result.err:
        raise SalesPDFError(
            f"PDF generation failed with {result.err} rendering error(s)."
        )
    return output.getvalue()


def invalidate_document_pdf(document):
    if document.pdf_file:
        try:
            document.pdf_file.delete(save=False)
        except Exception:
            # A missing/stale storage object must not block editing a draft.
            pass
    document.pdf_file = ""
    document.pdf_sha256 = ""
    document.pdf_generated_at = None


def ensure_document_pdf(document, *, actor=None, force=False):
    if (
        document.pdf_file
        and document.pdf_sha256
        and not force
    ):
        return document.pdf_file

    data = build_pdf_bytes(document)
    digest = hashlib.sha256(data).hexdigest()
    filename = get_valid_filename(f"{document.document_number}.pdf") or "document.pdf"

    if document.pdf_file:
        try:
            document.pdf_file.delete(save=False)
        except Exception:
            pass

    document.pdf_file.save(filename, ContentFile(data), save=False)
    document.pdf_sha256 = digest
    document.pdf_generated_at = timezone.now()
    document.save(
        update_fields=[
            "pdf_file",
            "pdf_sha256",
            "pdf_generated_at",
            "updated_at",
        ]
    )
    record_activity(
        document,
        event_type="pdf_generated",
        message=f"PDF generated for {document.document_number}.",
        actor=actor,
        metadata={"sha256": digest},
    )
    return document.pdf_file


def read_document_pdf(document, *, actor=None):
    ensure_document_pdf(document, actor=actor)
    document.pdf_file.open("rb")
    try:
        return document.pdf_file.read()
    finally:
        document.pdf_file.close()
