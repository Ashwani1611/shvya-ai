"""Email-safe presentation shared by previews and tracked delivery."""

import html
from urllib.parse import urljoin, urlparse

from django.template.loader import render_to_string

from apps.sales.document_services import HEX_COLOR_RE


def email_layout(document, body_html, *, base_url=""):
    presentation = dict(document.presentation_snapshot or {})
    for key in ("logo_url", "signature_url"):
        url = (
            urljoin(str(base_url).rstrip("/") + "/", presentation.get(key) or "")
            if presentation.get(key)
            else ""
        )
        presentation[key] = (
            url if urlparse(url).scheme in {"http", "https", "blob"} else ""
        )
    color = presentation.get("accent_color", "")
    presentation["accent_color"] = color if HEX_COLOR_RE.fullmatch(color) else "#0071e3"
    return render_to_string(
        "sales/email_message.html",
        {
            "document": document,
            "presentation": presentation,
            "body_html": body_html,
        },
    )


def email_preview(document, body):
    return email_layout(document, html.escape(body).replace("\n", "<br>"))
