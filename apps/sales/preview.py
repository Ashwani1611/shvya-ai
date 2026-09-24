"""Read-only template previews using the production rendering functions."""

from datetime import date

from django.http import HttpResponseForbidden, JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.sales.access import is_sales_admin
from apps.sales.document_services import (
    default_template_body,
    delivery_drafts,
    normalize_item_table_config,
    snapshot_document_presentation,
)
from apps.sales.email_design import email_preview
from apps.sales.models import DocumentType, SalesDocument, SalesTemplate


@crm_login_required
@require_POST
def template_preview(request):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can preview Sales templates."
        )
    org = request.crm_user.organization
    data = request.POST
    kind = data.get("document_type", DocumentType.QUOTATION)
    if kind not in DocumentType.values:
        kind = DocumentType.QUOTATION
    config = normalize_item_table_config(
        {
            **{
                key: data.get("item_" + key) == "on"
                for key in (
                    "show_name",
                    "show_description",
                    "description_separate",
                    "show_qty",
                    "show_rate",
                    "show_tax",
                    "show_amount",
                    "show_summary",
                )
            },
            "labels": {
                key: data.get("item_label_" + key)
                for key in (
                    "name",
                    "description",
                    "qty",
                    "rate",
                    "tax",
                    "amount",
                    "subtotal",
                    "tax_total",
                    "discount",
                    "total",
                )
            },
        }
    )
    template = SalesTemplate(
        organization=org,
        document_type=kind,
        body_template=data.get("body_template") or default_template_body(kind),
        item_table_config=config,
        **{
            key: data.get(key, "")
            for key in (
                "header_text",
                "footer_text",
                "logo_url",
                "signature_url",
                "accent_color",
                "email_subject_template",
                "email_body_template",
                "whatsapp_body_template",
            )
        },
    )
    lead = Lead(
        organization=org,
        name="Alex Morgan",
        email="alex@example.com",
        phone="+919876543210",
        attributes={},
    )
    document = SalesDocument(
        organization=org,
        template=template,
        lead=lead,
        document_type=kind,
        document_number=(data.get("number_prefix") or "PREVIEW") + "-00001",
        title=f"{DocumentType(kind).label} for Alex Morgan",
        issue_date=date.today(),
        due_date=date.today(),
        valid_until=date.today(),
        recipient_name=lead.name,
        recipient_email=lead.email,
        recipient_phone=lead.phone,
        currency="INR",
        content="A personalised solution prepared for your business.",
        terms="Payment is due within 15 days. Thank you for choosing us.",
        subtotal="10000.00",
        tax_total="1800.00",
        total="11800.00",
        discount_total="0.00",
        line_items=[
            {
                "name": "Professional services",
                "description": "Implementation and support",
                "qty": "1",
                "rate": "10000.00",
                "tax_rate": "18",
                "amount": "10000.00",
                "tax_amount": "1800.00",
            }
        ],
    )
    snapshot_document_presentation(document, public_url="https://example.com/document")
    drafts = delivery_drafts(document, public_url="https://example.com/document")
    return JsonResponse(
        {
            "html": render_to_string(
                "sales/template_preview.html",
                {
                    "document": document,
                    "presentation": document.presentation_snapshot,
                },
            ),
            "email_html": email_preview(document, drafts["email_body"]),
            **drafts,
        }
    )
