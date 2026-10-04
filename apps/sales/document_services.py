from __future__ import annotations

import html
import re
import warnings
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import get_valid_filename

from apps.sales.models import (
    DocumentType,
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
# Older default message bodies lost one brace pair through f-string escaping.
# Match those saved fields too, without treating JSON/CSS blocks as fields.
TEXT_MERGE_RE = re.compile(
    r"{{\s*([^{}]+?)\s*}}|(?<!{){\s*([A-Za-z_]\w*(?:\.\w+)*)\s*}(?!})"
)
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

    # Filename extensions and browser MIME types can disagree with the actual
    # image (for example, a downloaded WebP named logo.png). Validate the bytes
    # and normalize metadata instead of rejecting an otherwise valid upload.
    formats = {
        "PNG": (".png", "image/png"),
        "JPEG": (".jpg", "image/jpeg"),
        "WEBP": (".webp", "image/webp"),
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            uploaded_file.seek(0)
            with Image.open(uploaded_file, formats=list(formats)) as image:
                image_format = image.format
                image.verify()
            uploaded_file.seek(0)
            with Image.open(uploaded_file, formats=list(formats)) as image:
                image.load()
    except (
        UnidentifiedImageError, OSError, SyntaxError, ValueError,
        Image.DecompressionBombError, Image.DecompressionBombWarning,
    ) as exc:
        raise ValidationError(
            f"{label} must be a valid, undamaged PNG, JPG, JPEG or WEBP image."
        ) from exc
    finally:
        uploaded_file.seek(0)

    actual_extension, content_type = formats[image_format]
    if extension != actual_extension and not (image_format == "JPEG" and extension == ".jpeg"):
        original_name = str(Path(original_name).with_suffix(actual_extension))
    uploaded_file.content_type = content_type

    uploaded_file.name = (
        get_valid_filename(original_name)
        or f"{label.lower().replace(' ', '-')}{actual_extension}"
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
        f"Your {label} {{{{document.number}}}} is ready.\n"
        "Amount: {{document.total}}\n\n"
        "View securely: {{document.url}}\n\n"
        "Regards,\n{{organization.name}}"
    )


def default_whatsapp_body(document_type):
    label = DocumentType(document_type).label.lower()
    return (
        "Hi {{recipient.name}},\n\n"
        f"Your {label} {{{{document.number}}}} is ready.\n"
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
    if document.document_type == DocumentType.INVOICE and not document._state.adding:
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
    values["items_table"] = "\n".join(
        f"{item.get('name', '')} — {item.get('qty', '')} × {document.currency} {item.get('rate', '')} = {document.currency} {item.get('amount', '')}"
        for item in (document.line_items or [])
    )
    values.update({
        "document.content": document.content or "",
        "document.terms": document.terms or "",
    })
    # The same names work in layouts, email drafts and WhatsApp drafts.
    aliases = {
        "org_name": "organization.name", "lead_name": "lead.name",
        "phone": "recipient.phone", "email": "recipient.email",
        "recipient_name": "recipient.name", "pipeline_name": "lead.pipeline",
        "stage_name": "lead.stage", "document_number": "document.number",
        "document_title": "document.title", "document_type": "document.type",
        "document_total": "document.total", "document_url": "document.url",
        "document_due_date": "document.due_date",
        "document_valid_until": "document.valid_until",
    }
    values.update({alias: values[key] for alias, key in aliases.items()})
    values["lead_first_name"] = str(values["lead.name"] or values["recipient.name"]).split(" ")[0]
    for key in ("paid", "refunded", "credits", "balance", "payment_url"):
        values.setdefault(f"invoice.{key}", "")
    for key in ("document.content", "document.terms"):
        values[key] = render_text_template(values[key], {
            name: value for name, value in values.items()
            if name not in {"document.content", "document.terms"}
        })
    return values


def render_text_template(source, values, *, strict=False):
    def replace(match):
        key = (match.group(1) or match.group(2)).strip()
        value = values.get(key, match.group(0))
        return str(value if value is not None else "")

    rendered = TEXT_MERGE_RE.sub(replace, str(source or ""))
    if strict:
        unresolved = sorted({
            (match.group(1) or match.group(2)).strip()
            for match in TEXT_MERGE_RE.finditer(rendered)
        })
        if unresolved:
            raise SalesDeliveryError("Resolve these template variables before sending: " + ", ".join(unresolved))
    return rendered


def render_document_html(document, *, public_url=""):
    values = merge_values(document, public_url=public_url)
    values["items_table"] = _items_table(document)
    values["document.content"] = html.escape(values["document.content"]).replace("\n", "<br>")
    values["document.terms"] = html.escape(values["document.terms"]).replace("\n", "<br>")

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
        value = str(values[key] if values[key] is not None else "")
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
        "header_text": render_text_template(template.header_text, merge_values(document, public_url=public_url)) if template else document.organization.name,
        "footer_text": render_text_template(template.footer_text, merge_values(document, public_url=public_url)) if template else "",
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
