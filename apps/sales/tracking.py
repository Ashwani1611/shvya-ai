from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlparse

from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from apps.sales.activity import record_activity
from apps.sales.models import SalesDocumentDelivery
from apps.sales.models_lifecycle import SalesEmailTrackedLink


_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _absolute(base_url, path):
    return urljoin(str(base_url or "").rstrip("/") + "/", path.lstrip("/"))


def build_tracked_email_html(*, delivery, body, base_url):
    """Escape a plain-text draft, track HTTP(S) links, and append an open pixel."""
    text = str(body or "")
    chunks = []
    cursor = 0
    seen = {}

    for match in _URL_RE.finditer(text):
        chunks.append(html.escape(text[cursor:match.start()]))
        raw_url = match.group(0)
        # Avoid swallowing common sentence punctuation.
        target = raw_url.rstrip(".,);]}")
        trailing = raw_url[len(target):]
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            chunks.append(html.escape(raw_url))
            cursor = match.end()
            continue

        link = seen.get(target)
        if link is None:
            link = SalesEmailTrackedLink.objects.create(
                delivery=delivery,
                target_url=target,
            )
            seen[target] = link

        click_path = reverse("shvya-sales-email-click", args=[link.token])
        tracked_url = _absolute(base_url, click_path)
        chunks.append(
            f'<a href="{html.escape(tracked_url, quote=True)}" '
            'style="color:#0071e3;text-decoration:underline">'
            f"{html.escape(target)}</a>{html.escape(trailing)}"
        )
        cursor = match.end()

    chunks.append(html.escape(text[cursor:]))
    body_html = "".join(chunks).replace("\n", "<br>")

    pixel_path = reverse(
        "shvya-sales-email-open",
        args=[delivery.tracking_token],
    )
    pixel_url = _absolute(base_url, pixel_path)
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI',sans-serif;line-height:1.6;color:#1d1d1f\">"
        f"{body_html}"
        f'<img src="{html.escape(pixel_url, quote=True)}" width="1" height="1" '
        'alt="" style="display:block;width:1px;height:1px;border:0">'
        "</div>"
    )


@transaction.atomic
def record_email_open(*, tracking_token):
    delivery = (
        SalesDocumentDelivery.objects.select_for_update()
        .select_related("document")
        .filter(
            tracking_token=tracking_token,
            channel=SalesDocumentDelivery.Channel.EMAIL,
        )
        .first()
    )
    if delivery is None:
        return None

    now = timezone.now()
    first = delivery.opened_at is None
    delivery.open_count = F("open_count") + 1
    update_fields = ["open_count"]
    if first:
        delivery.opened_at = now
        update_fields.append("opened_at")
    delivery.save(update_fields=update_fields)
    delivery.refresh_from_db(fields=["open_count", "opened_at"])

    if first:
        record_activity(
            delivery.document,
            event_type="email_opened",
            message=f"Email for {delivery.document.document_number} was opened.",
            metadata={"delivery_id": str(delivery.id)},
        )
    return delivery


@transaction.atomic
def record_email_click(*, token):
    link = (
        SalesEmailTrackedLink.objects.select_for_update()
        .select_related("delivery__document")
        .filter(token=token)
        .first()
    )
    if link is None:
        return None

    now = timezone.now()
    first_link_click = link.first_clicked_at is None
    link.click_count = F("click_count") + 1
    link.last_clicked_at = now
    link_fields = ["click_count", "last_clicked_at"]
    if first_link_click:
        link.first_clicked_at = now
        link_fields.append("first_clicked_at")
    link.save(update_fields=link_fields)

    delivery = SalesDocumentDelivery.objects.select_for_update().get(
        pk=link.delivery_id
    )
    first_delivery_click = delivery.first_clicked_at is None
    delivery.click_count = F("click_count") + 1
    delivery_fields = ["click_count"]
    if first_delivery_click:
        delivery.first_clicked_at = now
        delivery_fields.append("first_clicked_at")
    delivery.save(update_fields=delivery_fields)

    if first_delivery_click:
        record_activity(
            delivery.document,
            event_type="email_clicked",
            message=f"A tracked email link for {delivery.document.document_number} was clicked.",
            metadata={
                "delivery_id": str(delivery.id),
                "target": link.target_url,
            },
        )
    return link


@transaction.atomic
def record_provider_email_event(
    *,
    delivery,
    event,
    reason="",
):
    """Apply authenticated provider delivery/bounce callbacks to one delivery."""
    delivery = (
        SalesDocumentDelivery.objects.select_for_update()
        .select_related("document")
        .get(pk=delivery.pk)
    )
    now = timezone.now()
    event = str(event or "").strip().lower()

    if event in {"delivered", "delivery"}:
        first = delivery.delivered_at is None
        if first:
            delivery.delivered_at = now
            delivery.save(update_fields=["delivered_at"])
            record_activity(
                delivery.document,
                event_type="email_delivered",
                message=f"Email for {delivery.document.document_number} was reported delivered.",
                metadata={"delivery_id": str(delivery.id)},
            )
    elif event in {"bounce", "bounced", "dropped", "rejected"}:
        first = delivery.bounced_at is None
        delivery.bounced_at = delivery.bounced_at or now
        delivery.bounce_reason = str(reason or "")[:1500]
        delivery.status = SalesDocumentDelivery.Status.FAILED
        delivery.error_message = delivery.bounce_reason or "Email bounced."
        delivery.save(
            update_fields=[
                "bounced_at",
                "bounce_reason",
                "status",
                "error_message",
            ]
        )
        if first:
            record_activity(
                delivery.document,
                event_type="email_bounced",
                message=f"Email for {delivery.document.document_number} bounced.",
                metadata={"delivery_id": str(delivery.id)},
            )
    return delivery
