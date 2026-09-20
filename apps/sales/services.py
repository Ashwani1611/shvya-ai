from __future__ import annotations

import html
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from bs4 import BeautifulSoup
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import get_valid_filename

from apps.sales.models import (
    DocumentType,
    SalesDocument,
    SalesDocumentDelivery,
    SalesDocumentNumberSequence,
    SalesTemplate,
)


MONEY = Decimal("0.01")
MAX_LINE_ITEMS = 250
MAX_QUANTITY = Decimal("1000000000")
MAX_DOCUMENT_AMOUNT = Decimal("999999999999.99")
MAX_TAX_RATE = Decimal("10000")
DEFAULT_PREFIXES = {
    DocumentType.QUOTATION: "QT",
    DocumentType.AGREEMENT: "AGR",
    DocumentType.INVOICE: "INV",
}
MERGE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_BRAND_ASSET_BYTES = 5 * 1024 * 1024
ALLOWED_BRAND_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

DEFAULT_ITEM_TABLE_CONFIG = {
    "show_name": True,
    "show_description": True,
    "description_separate": False,
    "show_qty": True,
    "show_rate": True,
    "show_tax": True,
    "show_amount": True,
    "show_summary": True,
    "labels": {
        "name": "Item",
        "description": "Description",
        "qty": "Qty",
        "rate": "Rate",
        "tax": "Tax",
        "amount": "Amount",
        "subtotal": "Subtotal",
        "tax_total": "Tax",
        "discount": "Discount",
        "total": "Total",
    },
}

_ALLOWED_TAGS = {
    "div",
    "section",
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "strong",
    "em",
    "b",
    "i",
    "small",
    "span",
    "br",
    "hr",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "ul",
    "ol",
    "li",
}
_ALLOWED_STYLE_PROPERTIES = {
    "text-align",
    "color",
    "background-color",
    "font-size",
    "font-weight",
    "line-height",
    "margin",
    "margin-top",
    "margin-bottom",
    "padding",
    "padding-top",
    "padding-bottom",
    "border",
    "border-top",
    "border-bottom",
    "border-radius",
    "width",
    "max-width",
}


class SalesDeliveryError(RuntimeError):
    pass


def validate_brand_asset(uploaded_file, *, label):
    """Validate organization-uploaded logo/signature images before storage."""
    if uploaded_file is None:
        return None
    if uploaded_file.size <= 0:
        raise ValidationError(f"{label} file is empty.")
    if uploaded_file.size > MAX_BRAND_ASSET_BYTES:
        raise ValidationError(f"{label} must be 5 MB or smaller.")

    original_name = Path(uploaded_file.name or label).name
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_BRAND_ASSET_EXTENSIONS:
        raise ValidationError(f"{label} must be PNG, JPG, JPEG or WEBP.")

    content_type = str(getattr(uploaded_file, "content_type", "") or "").split(";", 1)[0]
    if not content_type.startswith("image/"):
        raise ValidationError(f"{label} must be a valid image file.")

    position = uploaded_file.tell() if hasattr(uploaded_file, "tell") else 0
    header = uploaded_file.read(16)
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(position)

    is_png = extension == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = extension in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff")
    is_webp = (
        extension == ".webp"
        and len(header) >= 12
        and header[:4] == b"RIFF"
        and header[8:12] == b"WEBP"
    )
    if not (is_png or is_jpeg or is_webp):
        raise ValidationError(f"{label} content does not match its image file type.")

    uploaded_file.name = (
        get_valid_filename(original_name)
        or f"{label.lower().replace(' ', '-')}{extension}"
    )
    return uploaded_file


def _decimal(value, *, default="0"):
    try:
        result = Decimal(str(value if value not in (None, "") else default))
        if not result.is_finite():
            raise InvalidOperation
        return result.quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(
            "Enter valid finite numeric values for quantity, rate, tax and discount."
        ) from exc


def calculate_line_items(items, *, discount_total=0):
    """Normalize client line items and calculate all financial totals server-side."""
    normalized = []
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")

    if not isinstance(items, list):
        raise ValidationError("Line items must be a list.")
    if len(items) > MAX_LINE_ITEMS:
        raise ValidationError(f"A document can contain at most {MAX_LINE_ITEMS} line items.")

    for position, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        description = str(raw.get("description") or "").strip()
        if len(name) > 300:
            raise ValidationError("Line item names must be 300 characters or fewer.")
        if len(description) > 2000:
            raise ValidationError("Line item descriptions must be 2,000 characters or fewer.")

        qty = _decimal(raw.get("qty"), default="1")
        rate = _decimal(raw.get("rate"))
        tax_rate = _decimal(raw.get("tax_rate"))
        if qty < 0 or rate < 0 or tax_rate < 0:
            raise ValidationError("Quantity, rate and tax cannot be negative.")
        if qty > MAX_QUANTITY:
            raise ValidationError("Line item quantity is too large.")
        if rate > MAX_DOCUMENT_AMOUNT:
            raise ValidationError("Line item rate is too large.")
        if tax_rate > MAX_TAX_RATE:
            raise ValidationError("Line item tax percentage is too large.")

        amount = (qty * rate).quantize(MONEY, rounding=ROUND_HALF_UP)
        if amount > MAX_DOCUMENT_AMOUNT:
            raise ValidationError("A line item amount exceeds the supported document limit.")
        tax_amount = (amount * tax_rate / Decimal("100")).quantize(
            MONEY,
            rounding=ROUND_HALF_UP,
        )
        subtotal += amount
        tax_total += tax_amount
        if subtotal + tax_total > MAX_DOCUMENT_AMOUNT:
            raise ValidationError("Document total exceeds the supported amount limit.")

        normalized.append(
            {
                "position": position,
                "name": name,
                "description": description,
                "qty": str(qty),
                "rate": str(rate),
                "tax_rate": str(tax_rate),
                "amount": str(amount),
                "tax_amount": str(tax_amount),
            }
        )

    discount = _decimal(discount_total)
    if discount < 0:
        raise ValidationError("Discount cannot be negative.")
    if discount > subtotal + tax_total:
        raise ValidationError("Discount cannot exceed the document amount.")

    total = (subtotal + tax_total - discount).quantize(MONEY, rounding=ROUND_HALF_UP)
    return {
        "items": normalized,
        "subtotal": subtotal.quantize(MONEY),
        "tax_total": tax_total.quantize(MONEY),
        "discount_total": discount,
        "total": total,
    }


def _sanitize_style(value):
    safe = []
    for part in str(value or "").split(";"):
        if ":" not in part:
            continue
        key, raw_value = part.split(":", 1)
        key = key.strip().lower()
        raw_value = raw_value.strip()
        lowered = raw_value.lower()
        if key not in _ALLOWED_STYLE_PROPERTIES:
            continue
        if "url(" in lowered or "expression(" in lowered or "javascript:" in lowered:
            continue
        safe.append(f"{key}: {raw_value}")
    return "; ".join(safe)


def sanitize_layout_html(value):
    """Allow flexible formatting without allowing scripts/event handlers into public pages."""
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for node in soup.find_all(["script", "iframe", "object", "embed", "form", "input", "button"]):
        node.decompose()

    for tag in list(soup.find_all(True)):
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue

        attrs = {}
        if tag.has_attr("style"):
            style = _sanitize_style(tag.get("style"))
            if style:
                attrs["style"] = style
        if tag.name in {"td", "th"}:
            for attr in ("colspan", "rowspan"):
                raw = str(tag.get(attr) or "").strip()
                if raw.isdigit() and 1 <= int(raw) <= 12:
                    attrs[attr] = raw
        tag.attrs = attrs

    return str(soup)


def normalize_item_table_config(value):
    raw = value if isinstance(value, dict) else {}
    config = {
        key: bool(raw.get(key, default))
        for key, default in DEFAULT_ITEM_TABLE_CONFIG.items()
        if key != "labels"
    }
    labels_raw = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    labels = {}
    for key, default in DEFAULT_ITEM_TABLE_CONFIG["labels"].items():
        text = str(labels_raw.get(key) or default).strip()
        labels[key] = text[:40] or default
    config["labels"] = labels

    # Never render a structurally empty line-item table.
    if not any(
        config.get(key)
        for key in (
            "show_name",
            "show_description",
            "show_qty",
            "show_rate",
            "show_tax",
            "show_amount",
        )
    ):
        config["show_name"] = True
    if not config["show_description"]:
        config["description_separate"] = False
    return config


def default_item_table_config():
    return normalize_item_table_config(DEFAULT_ITEM_TABLE_CONFIG)


def default_template_body(document_type):
    intro = {
        DocumentType.QUOTATION: "We are pleased to share the following quotation.",
        DocumentType.AGREEMENT: "This agreement records the scope and terms accepted by the parties.",
        DocumentType.INVOICE: "Please find the invoice details below.",
    }[document_type]
    return (
        '<section style="padding: 8px 0 22px">'
        '<h1 style="font-size: 34px; margin-bottom: 8px">{{document.title}}</h1>'
        '<p style="color: #6e6e73">{{document.number}} · {{document.issue_date}}</p>'
        f'<p style="margin-top: 24px">{intro}</p>'
        '<div style="margin-top: 28px">{{document.content}}</div>'
        '<div style="margin-top: 28px">{{items_table}}</div>'
        '<div style="margin-top: 28px">{{document.terms}}</div>'
        "</section>"
    )


def default_email_subject(document_type):
    label = DocumentType(document_type).label
    return f"{label} {{{{document.number}}}} from {{{{organization.name}}}}"


def default_email_body(document_type):
    label = DocumentType(document_type).label.lower()
    return (
        "Hi {{recipient.name}},\n\n"
        f"Your {label} {{document.number}} is ready.\n"
        "Amount: {{document.total}}\n\n"
        "View securely: {{document.url}}\n\n"
        "Regards,\n{{organization.name}}"
    )


def default_whatsapp_body(document_type):
    label = DocumentType(document_type).label.lower()
    return (
        "Hi {{recipient.name}},\n\n"
        f"Your {label} {{document.number}} is ready.\n"
        "Amount: {{document.total}}\n"
        "{{document.url}}\n\n"
        "— {{organization.name}}"
    )


def ensure_default_templates(*, organization, user=None):
    created = []
    for document_type in DocumentType.values:
        if SalesTemplate.objects.filter(
            organization=organization,
            document_type=document_type,
            is_active=True,
        ).exists():
            continue
        template = SalesTemplate.objects.create(
            organization=organization,
            document_type=document_type,
            name=f"Standard {DocumentType(document_type).label}",
            number_prefix=DEFAULT_PREFIXES[document_type],
            header_text=organization.name,
            body_template=default_template_body(document_type),
            item_table_config=default_item_table_config(),
            footer_text="Thank you for your business.",
            email_subject_template=default_email_subject(document_type),
            email_body_template=default_email_body(document_type),
            whatsapp_body_template=default_whatsapp_body(document_type),
            is_default=True,
            created_by=user,
        )
        created.append(template)
    return created


@transaction.atomic
def next_document_number(*, organization, document_type, prefix=""):
    prefix = str(prefix or DEFAULT_PREFIXES[document_type]).strip().upper()[:12]
    sequence, _ = SalesDocumentNumberSequence.objects.get_or_create(
        organization=organization,
        document_type=document_type,
        defaults={"next_number": 1},
    )
    sequence = SalesDocumentNumberSequence.objects.select_for_update().get(pk=sequence.pk)
    value = sequence.next_number
    sequence.next_number = value + 1
    sequence.save(update_fields=["next_number"])
    return f"{prefix}-{value:05d}"


def _money_text(value, currency):
    return f"{currency} {Decimal(value or 0):,.2f}"


def _items_table(document):
    if not document.line_items:
        return ""

    snapshot = (
        document.presentation_snapshot
        if isinstance(document.presentation_snapshot, dict)
        else {}
    )
    config = normalize_item_table_config(
        snapshot.get("item_table_config")
        or (
            document.template.item_table_config
            if document.template_id and document.template
            else {}
        )
    )
    labels = config["labels"]

    columns = []
    if config["show_name"]:
        columns.append(("name", labels["name"]))
    if config["show_description"] and config["description_separate"]:
        columns.append(("description", labels["description"]))
    if config["show_qty"]:
        columns.append(("qty", labels["qty"]))
    if config["show_rate"]:
        columns.append(("rate", labels["rate"]))
    if config["show_tax"]:
        columns.append(("tax", labels["tax"]))
    if config["show_amount"]:
        columns.append(("amount", labels["amount"]))

    headers = "".join(
        f"<th>{html.escape(label)}</th>"
        for _key, label in columns
    )
    rows = []
    for item in document.line_items:
        cells = []
        for key, _label in columns:
            if key == "name":
                value = html.escape(str(item.get("name") or ""))
                if (
                    config["show_description"]
                    and not config["description_separate"]
                    and item.get("description")
                ):
                    value += (
                        "<small>"
                        + html.escape(str(item.get("description") or ""))
                        + "</small>"
                    )
            elif key == "description":
                value = html.escape(str(item.get("description") or ""))
            elif key == "qty":
                value = html.escape(str(item.get("qty") or ""))
            elif key == "rate":
                value = html.escape(str(item.get("rate") or ""))
            elif key == "tax":
                value = html.escape(str(item.get("tax_rate") or "0")) + "%"
            else:
                value = html.escape(str(item.get("amount") or ""))
            cells.append(f"<td>{value}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")

    summary = ""
    if config["show_summary"]:
        summary = (
            f'<div class="sales-total-row"><span>{html.escape(labels["subtotal"])}</span>'
            f"<strong>{_money_text(document.subtotal, document.currency)}</strong></div>"
            f'<div class="sales-total-row"><span>{html.escape(labels["tax_total"])}</span>'
            f"<strong>{_money_text(document.tax_total, document.currency)}</strong></div>"
            f'<div class="sales-total-row"><span>{html.escape(labels["discount"])}</span>'
            f"<strong>- {_money_text(document.discount_total, document.currency)}</strong></div>"
            f'<div class="sales-total-row sales-total-final"><span>{html.escape(labels["total"])}</span>'
            f"<strong>{_money_text(document.total, document.currency)}</strong></div>"
        )

    return (
        '<div class="sales-items-wrap"><table class="sales-items-table">'
        f"<thead><tr>{headers}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f'<div class="sales-totals">{summary}</div></div>'
    )




def merge_values(document, *, public_url=""):
    lead = document.lead
    values = {
        "organization.name": document.organization.name,
        "recipient.name": document.recipient_name or (lead.name if lead else ""),
        "recipient.email": document.recipient_email or (lead.email if lead else ""),
        "recipient.phone": document.recipient_phone or (lead.phone if lead else ""),
        "lead.name": lead.name if lead else "",
        "lead.email": lead.email if lead else "",
        "lead.phone": lead.phone if lead else "",
        "lead.pipeline": lead.pipeline.name if lead and lead.pipeline_id else "",
        "lead.stage": lead.stage.name if lead and lead.stage_id else "",
        "document.type": document.get_document_type_display(),
        "document.title": document.title,
        "document.number": document.document_number,
        "document.issue_date": document.issue_date.isoformat() if document.issue_date else "",
        "document.valid_until": document.valid_until.isoformat() if document.valid_until else "",
        "document.due_date": document.due_date.isoformat() if document.due_date else "",
        "document.subtotal": _money_text(document.subtotal, document.currency),
        "document.tax": _money_text(document.tax_total, document.currency),
        "document.discount": _money_text(document.discount_total, document.currency),
        "document.total": _money_text(document.total, document.currency),
        "document.currency": document.currency,
        "document.url": public_url,
    }
    if lead:
        for key, value in (lead.attributes or {}).items():
            values[f"lead.attribute.{key}"] = value
    if document.document_type == DocumentType.INVOICE:
        from apps.sales.lifecycle import invoice_ledger
        from apps.sales.models_lifecycle import SalesPaymentCheckout

        ledger = invoice_ledger(document)
        checkout = document.payment_checkouts.filter(
            status=SalesPaymentCheckout.Status.CREATED,
        ).first()
        values.update(
            {
                "invoice.paid": _money_text(
                    ledger["paid"],
                    document.currency,
                ),
                "invoice.refunded": _money_text(
                    ledger["refunded"],
                    document.currency,
                ),
                "invoice.credits": _money_text(
                    ledger["credits"],
                    document.currency,
                ),
                "invoice.balance": _money_text(
                    ledger["balance"],
                    document.currency,
                ),
                "invoice.payment_url": (
                    checkout.checkout_url if checkout else ""
                ),
            }
        )
    return values


def render_text_template(source, values):
    def replace(match):
        key = match.group(1).strip()
        value = values.get(key, match.group(0))
        return str(value if value is not None else "")

    return MERGE_RE.sub(replace, str(source or ""))


def render_document_html(document, *, public_url=""):
    values = merge_values(document, public_url=public_url)
    values["items_table"] = _items_table(document)
    values["document.content"] = html.escape(document.content or "").replace("\n", "<br>")
    values["document.terms"] = html.escape(document.terms or "").replace("\n", "<br>")

    source = (
        document.layout_override
        or (
            document.template.body_template
            if document.template_id and document.template
            else default_template_body(document.document_type)
        )
    )
    source = sanitize_layout_html(source)
    safe_keys = {"items_table", "document.content", "document.terms"}

    def replace(match):
        key = match.group(1).strip()
        if key not in values:
            return match.group(0)
        value = str(values.get(key) or "")
        return value if key in safe_keys else html.escape(value)

    return MERGE_RE.sub(replace, source)


def snapshot_document_presentation(document, *, public_url=""):
    template = document.template
    logo_url = ""
    signature_url = ""
    if template:
        logo_url = template.logo_file.url if template.logo_file else template.logo_url
        signature_url = (
            template.signature_file.url if template.signature_file else template.signature_url
        )

    snapshot = {
        "template_name": template.name if template else "Standard",
        "logo_url": logo_url,
        "signature_url": signature_url,
        "accent_color": (
            template.accent_color
            if template and HEX_COLOR_RE.match(template.accent_color or "")
            else "#0071e3"
        ),
        "header_text": template.header_text if template else document.organization.name,
        "footer_text": template.footer_text if template else "",
        "item_table_config": normalize_item_table_config(
            template.item_table_config if template else {}
        ),
        "custom_layout": bool(document.layout_override),
    }
    document.presentation_snapshot = snapshot
    document.rendered_html = render_document_html(document, public_url=public_url)
    document.email_subject_snapshot = (
        template.email_subject_template if template and template.email_subject_template
        else default_email_subject(document.document_type)
    )
    document.email_body_snapshot = (
        template.email_body_template if template and template.email_body_template
        else default_email_body(document.document_type)
    )
    document.whatsapp_body_snapshot = (
        template.whatsapp_body_template if template and template.whatsapp_body_template
        else default_whatsapp_body(document.document_type)
    )
    return document


def delivery_drafts(document, *, public_url):
    values = merge_values(document, public_url=public_url)
    return {
        "email_subject": render_text_template(document.email_subject_snapshot, values),
        "email_body": render_text_template(document.email_body_snapshot, values),
        "whatsapp_body": render_text_template(document.whatsapp_body_snapshot, values),
    }


def _mark_document_sent(document, *, actor=None):
    old_status = document.status
    was_unsent = document.sent_at is None
    if document.status == SalesDocument.Status.DRAFT:
        document.status = (
            SalesDocument.Status.UNPAID
            if document.document_type == DocumentType.INVOICE
            else SalesDocument.Status.SENT
        )
    if was_unsent:
        document.sent_at = timezone.now()
    document.save(update_fields=["status", "sent_at", "updated_at"])
    if old_status != document.status or was_unsent:
        from apps.sales.activity import record_activity

        record_activity(
            document,
            event_type="document_sent",
            message=f"{document.document_number} sent.",
            actor=actor,
            metadata={"status": document.status},
        )


def _record_failure(delivery, exc):
    delivery.status = SalesDocumentDelivery.Status.FAILED
    delivery.error_message = str(exc)[:1500]
    delivery.save(update_fields=["status", "error_message"])
    from apps.sales.activity import record_activity

    record_activity(
        delivery.document,
        event_type="delivery_failed",
        message=f"{delivery.get_channel_display()} delivery failed.",
        actor=delivery.sent_by,
        metadata={
            "delivery_id": str(delivery.id),
            "channel": delivery.channel,
        },
    )
    raise SalesDeliveryError(str(exc)) from exc


def _attachment_payloads(document):
    payloads = []
    try:
        settings_row = document.organization.sales_settings
    except Exception:
        settings_row = None
    if not settings_row or not settings_row.attach_customer_files_to_email:
        return payloads

    for attachment in document.attachments.filter(visible_to_customer=True)[:10]:
        try:
            attachment.file.open("rb")
            content = attachment.file.read()
        finally:
            try:
                attachment.file.close()
            except Exception:
                pass
        if len(content) > 10 * 1024 * 1024:
            continue
        payloads.append(
            (
                attachment.original_name,
                content,
                attachment.mime_type or "application/octet-stream",
            )
        )
    return payloads


def _delivery_row(
    *,
    document,
    channel,
    user,
    to_identity="",
    subject="",
    body="",
    scheduled_delivery=None,
    reminder=None,
):
    """Create one delivery, or recover the durable row for unattended work."""
    defaults = {
        "organization": document.organization,
        "document": document,
        "to_identity": str(to_identity or ""),
        "subject": str(subject or "").strip(),
        "body": str(body or "").strip(),
        "sent_by": user,
    }
    if scheduled_delivery is not None:
        delivery, created = SalesDocumentDelivery.objects.get_or_create(
            scheduled_delivery=scheduled_delivery,
            channel=channel,
            defaults=defaults,
        )
    elif reminder is not None:
        delivery, created = SalesDocumentDelivery.objects.get_or_create(
            reminder=reminder,
            channel=channel,
            defaults=defaults,
        )
    else:
        delivery = SalesDocumentDelivery.objects.create(
            channel=channel,
            **defaults,
        )
        created = True

    if created:
        return delivery, True

    if delivery.status == SalesDocumentDelivery.Status.SENT:
        return delivery, False
    if (
        channel == SalesDocumentDelivery.Channel.WHATSAPP
        and delivery.status == SalesDocumentDelivery.Status.QUEUED
        and delivery.provider_message_id
    ):
        return delivery, False

    raise SalesDeliveryError(
        "A previous automated delivery attempt already exists and was not "
        "resent automatically to avoid duplicates. Review its delivery history."
    )


def deliver_email(
    *,
    document,
    user,
    subject,
    body,
    base_url="",
    attach_pdf=True,
    scheduled_delivery=None,
    reminder=None,
):
    from apps.integrations.models import EmailConfiguration
    from apps.integrations.services.email import (
        EmailConfigurationError,
        send_organization_email,
    )
    from apps.sales.pdf_service import read_document_pdf
    from apps.sales.tracking import build_tracked_email_html

    delivery, created = _delivery_row(
        document=document,
        channel=SalesDocumentDelivery.Channel.EMAIL,
        user=user,
        to_identity=document.recipient_email,
        subject=subject,
        body=body,
        scheduled_delivery=scheduled_delivery,
        reminder=reminder,
    )
    if not created and delivery.status == SalesDocumentDelivery.Status.SENT:
        return delivery
    if not document.recipient_email:
        return _record_failure(
            delivery,
            SalesDeliveryError("This document has no recipient email address."),
        )
    try:
        validate_email(document.recipient_email)
    except ValidationError:
        return _record_failure(
            delivery,
            SalesDeliveryError("The recipient email address is invalid."),
        )

    configuration = EmailConfiguration.objects.filter(
        organization=document.organization,
        is_enabled=True,
        last_test_status=EmailConfiguration.TestStatus.SUCCESS,
    ).first()
    if not configuration:
        return _record_failure(
            delivery,
            SalesDeliveryError("Connect and test an email account in Connect Hub before sending."),
        )

    delivery.from_identity = configuration.email_address
    message_id = f"<sales-{delivery.id}@shvya.ai>"
    delivery.provider_message_id = message_id
    delivery.save(update_fields=["from_identity", "provider_message_id"])

    attachments = []
    if attach_pdf:
        try:
            pdf_bytes = read_document_pdf(document, actor=user)
        except Exception:
            return _record_failure(
                delivery,
                SalesDeliveryError("The PDF could not be generated for this send."),
            )
        attachments.append(
            (
                f"{document.document_number}.pdf",
                pdf_bytes,
                "application/pdf",
            )
        )
    attachments.extend(_attachment_payloads(document))

    tracked_html = build_tracked_email_html(
        delivery=delivery,
        body=delivery.body,
        base_url=base_url,
    )
    try:
        send_organization_email(
            organization=document.organization,
            to=document.recipient_email,
            subject=delivery.subject,
            text_body=delivery.body,
            html_body=tracked_html,
            headers={
                "X-SHVYA-Sales-Document": document.document_number,
                "X-SHVYA-Sales-Delivery": str(delivery.id),
                "Message-ID": message_id,
            },
            attachments=attachments,
        )
    except EmailConfigurationError as exc:
        return _record_failure(delivery, exc)

    delivery.status = SalesDocumentDelivery.Status.SENT
    delivery.sent_at = timezone.now()
    delivery.save(update_fields=["status", "sent_at"])
    _mark_document_sent(document, actor=user)

    from apps.sales.activity import record_activity

    record_activity(
        document,
        event_type="email_sent",
        message=f"Email sent to {document.recipient_email}.",
        actor=user,
        metadata={
            "delivery_id": str(delivery.id),
            "pdf_attached": bool(attach_pdf),
        },
    )
    return delivery


def _sales_whatsapp_template_values(*, document, user=None, public_url=""):
    lead = document.lead
    values = {
        "lead_name": lead.name if lead else "",
        "lead_first_name": (lead.name or "").split(" ")[0] if lead else "",
        "phone": lead.phone if lead else document.recipient_phone,
        "email": lead.email if lead else document.recipient_email,
        "lead_source": getattr(lead, "lead_source", "") if lead else "",
        "org_name": document.organization.name,
        "user_name": getattr(user, "name", "") or getattr(user, "email", "") or "",
        "pipeline_name": lead.pipeline.name if lead and lead.pipeline_id else "",
        "stage_name": lead.stage.name if lead and lead.stage_id else "",
        "document_number": document.document_number,
        "document_title": document.title,
        "document_type": document.get_document_type_display(),
        "document_total": _money_text(document.total, document.currency),
        "document_url": public_url,
        "document_due_date": document.due_date.isoformat() if document.due_date else "",
        "document_valid_until": (
            document.valid_until.isoformat() if document.valid_until else ""
        ),
    }
    if lead:
        values.update(getattr(lead, "attributes", None) or {})
    return values


def _sales_template_message(
    *,
    template,
    document,
    user,
    pdf_url,
    public_url,
):
    from services.channels.template_service import state_for

    state = state_for(template)
    mapping = (
        state.placeholder_mapping
        if isinstance(state.placeholder_mapping, dict)
        else {}
    )
    values = _sales_whatsapp_template_values(
        document=document,
        user=user,
        public_url=public_url,
    )
    components = [
        {
            "type": "header",
            "parameters": [
                {
                    "type": "document",
                    "document": {
                        "link": pdf_url,
                        "filename": f"{document.document_number}.pdf",
                    },
                }
            ],
        }
    ]
    rendered_body = str(template.body or "")
    if mapping:
        try:
            ordered_numbers = sorted(mapping, key=lambda value: int(value))
        except (TypeError, ValueError) as exc:
            raise SalesDeliveryError(
                "The selected WhatsApp template has invalid placeholder metadata."
            ) from exc
        parameters = []
        for number in ordered_numbers:
            key = mapping[number]
            value = str(values.get(key, "") or "")
            parameters.append({"type": "text", "text": value})
            rendered_body = rendered_body.replace(
                "{{" + str(key) + "}}",
                value,
            ).replace(
                "{{" + str(number) + "}}",
                value,
            )
        components.append({"type": "body", "parameters": parameters})

    return (
        rendered_body,
        {
            "transport": "template",
            "template_id": str(template.id),
            "template_name": template.name,
            "language_code": state.language or "en_US",
            "components": components,
        },
    )


def deliver_whatsapp(
    *,
    document,
    user,
    body,
    base_url="",
    attach_pdf=True,
    whatsapp_template_id=None,
    scheduled_delivery=None,
    reminder=None,
):
    from urllib.parse import urljoin

    from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
    from apps.channels.tasks import send_whatsapp_message_task
    from services.crm.lead_chat import pipeline_chat_account

    delivery, created = _delivery_row(
        document=document,
        channel=SalesDocumentDelivery.Channel.WHATSAPP,
        user=user,
        to_identity=document.recipient_phone,
        body=body,
        scheduled_delivery=scheduled_delivery,
        reminder=reminder,
    )
    if not created:
        return delivery
    if not document.lead_id:
        return _record_failure(
            delivery,
            SalesDeliveryError(
                "Link this document to a CRM lead before sending it on WhatsApp."
            ),
        )
    if not delivery.body and not whatsapp_template_id:
        return _record_failure(
            delivery,
            SalesDeliveryError("WhatsApp message cannot be empty."),
        )

    try:
        account = pipeline_chat_account(document.lead)
    except ValidationError as exc:
        message = "; ".join(getattr(exc, "messages", None) or [str(exc)])
        return _record_failure(delivery, SalesDeliveryError(message))

    delivery.from_identity = account.display_phone_number or account.phone_number_id
    delivery.to_identity = document.lead.phone
    delivery.save(update_fields=["from_identity", "to_identity"])

    try:
        from services.channels.whatsapp_service import queue_outbound_message

        public_path = reverse(
            "shvya-sales-public-document",
            args=[document.public_token],
        )
        public_url = urljoin(
            str(base_url or "").rstrip("/") + "/",
            public_path.lstrip("/"),
        )
        pdf_url = ""
        if attach_pdf:
            from apps.sales.pdf_service import ensure_document_pdf

            ensure_document_pdf(document, actor=user)
            pdf_path = reverse(
                "shvya-sales-public-pdf",
                args=[document.public_token],
            )
            pdf_url = urljoin(
                str(base_url or "").rstrip("/") + "/",
                pdf_path.lstrip("/"),
            )
            if not pdf_url.startswith(("http://", "https://")):
                raise SalesDeliveryError(
                    "A public HTTP(S) base URL is required to attach PDFs on WhatsApp."
                )

        requires_template = False
        if account.connection_type == WhatsAppAccount.ConnectionType.API:
            from services.channels.whatsapp_api_chat_service import (
                is_within_api_24h_window,
            )

            requires_template = not is_within_api_24h_window(
                lead=document.lead,
                account=account,
            )

        if requires_template:
            if not whatsapp_template_id:
                raise SalesDeliveryError(
                    "Meta requires an approved document-header WhatsApp template "
                    "outside the active 24-hour service window."
                )
            template = (
                WhatsAppTemplate.objects.select_related("account", "meta_state")
                .filter(
                    id=whatsapp_template_id,
                    organization=document.organization,
                    account=account,
                    status=WhatsAppTemplate.Status.APPROVED,
                    attachment_type=WhatsAppTemplate.AttachmentType.DOCUMENT,
                )
                .exclude(meta_template_id="")
                .first()
            )
            if template is None:
                raise SalesDeliveryError(
                    "Choose an approved document-header template for this "
                    "pipeline's linked WhatsApp number."
                )
            if not attach_pdf or not pdf_url:
                raise SalesDeliveryError(
                    "This approved WhatsApp template requires the document PDF."
                )
            rendered_body, template_payload = _sales_template_message(
                template=template,
                document=document,
                user=user,
                pdf_url=pdf_url,
                public_url=public_url,
            )
            delivery.body = rendered_body
            delivery.save(update_fields=["body"])
            message = queue_outbound_message(
                organization=document.organization,
                account=account,
                to_number=document.lead.phone,
                body=rendered_body or f"Document {document.document_number}",
                lead=document.lead,
                message_type=WhatsAppMessage.MessageType.TEXT,
                media_payload=template_payload,
            )
        elif attach_pdf:
            message = queue_outbound_message(
                organization=document.organization,
                account=account,
                to_number=document.lead.phone,
                body=delivery.body,
                lead=document.lead,
                message_type=WhatsAppMessage.MessageType.DOCUMENT,
                media_payload={
                    "source": "url",
                    "url": pdf_url,
                    "filename": f"{document.document_number}.pdf",
                    "caption": delivery.body,
                },
            )
        else:
            message = queue_outbound_message(
                organization=document.organization,
                account=account,
                to_number=document.lead.phone,
                body=delivery.body,
                lead=document.lead,
            )

        raw_payload = (
            message.raw_payload
            if isinstance(message.raw_payload, dict)
            else {}
        )
        raw_payload = dict(raw_payload)
        raw_payload["shvya_sales"] = {
            "document_id": str(document.id),
            "document_number": document.document_number,
            "delivery_id": str(delivery.id),
        }
        if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
            raw_payload.setdefault(
                "shvya_hosted",
                {"origin": "agent", "chat_id": document.lead.phone},
            )
        message.raw_payload = raw_payload
        message.save(update_fields=["raw_payload", "updated_at"])
        # Make tokenized document URLs public before the asynchronous provider
        # asks SHVYA to serve the PDF.
        _mark_document_sent(document, actor=user)
        send_whatsapp_message_task.delay(str(message.id))
    except SalesDeliveryError as exc:
        return _record_failure(delivery, exc)
    except Exception:
        return _record_failure(
            delivery,
            SalesDeliveryError(
                "WhatsApp delivery could not be queued. Review the linked "
                "number/template and try again."
            ),
        )

    delivery.provider_message_id = str(message.id)
    delivery.status = SalesDocumentDelivery.Status.QUEUED
    delivery.save(update_fields=["provider_message_id", "status"])

    from apps.sales.activity import record_activity

    record_activity(
        document,
        event_type="whatsapp_queued",
        message=f"WhatsApp delivery queued to {document.lead.phone}.",
        actor=user,
        metadata={
            "delivery_id": str(delivery.id),
            "whatsapp_message_id": str(message.id),
            "pdf_attached": bool(attach_pdf),
            "template_used": bool(requires_template),
        },
    )
    return delivery


def refresh_whatsapp_delivery_statuses(document):
    """Reflect the canonical WhatsApp message state in sales delivery history."""
    from apps.channels.models import WhatsAppMessage

    deliveries = list(
        document.deliveries.filter(
            channel=SalesDocumentDelivery.Channel.WHATSAPP,
        ).exclude(provider_message_id="")
    )
    ids = []
    for delivery in deliveries:
        try:
            ids.append(uuid.UUID(str(delivery.provider_message_id)))
        except (TypeError, ValueError, AttributeError):
            continue

    if not ids:
        return

    messages = {
        str(message.id): message
        for message in WhatsAppMessage.objects.filter(
            organization=document.organization,
            id__in=ids,
        )
    }
    successful_states = {
        WhatsAppMessage.Status.SENT,
        WhatsAppMessage.Status.DELIVERED,
        WhatsAppMessage.Status.READ,
    }

    for delivery in deliveries:
        message = messages.get(str(delivery.provider_message_id))
        if message is None:
            continue

        if message.status == WhatsAppMessage.Status.FAILED:
            desired_status = SalesDocumentDelivery.Status.FAILED
            desired_error = message.error or "WhatsApp delivery failed."
        elif message.status in successful_states:
            desired_status = SalesDocumentDelivery.Status.SENT
            desired_error = ""
        else:
            desired_status = SalesDocumentDelivery.Status.QUEUED
            desired_error = ""

        update_fields = []
        if delivery.status != desired_status:
            delivery.status = desired_status
            update_fields.append("status")
        if delivery.error_message != desired_error:
            delivery.error_message = desired_error
            update_fields.append("error_message")
        if desired_status == SalesDocumentDelivery.Status.SENT and delivery.sent_at is None:
            delivery.sent_at = timezone.now()
            update_fields.append("sent_at")
        if update_fields:
            delivery.save(update_fields=update_fields)


def public_url_for(document, request):
    path = reverse("shvya-sales-public-document", args=[document.public_token])
    return request.build_absolute_uri(path)
