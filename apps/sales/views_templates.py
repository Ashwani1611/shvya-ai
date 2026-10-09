# ruff: noqa: F401
import json
from datetime import datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_POST

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.integrations.models import EmailConfiguration
from apps.sales.access import is_sales_admin
from apps.sales.activity import record_activity
from apps.sales.lifecycle import (
    create_scheduled_delivery,
    invoice_ledger,
    invalidate_draft_artifacts,
)
from apps.sales.models import DocumentType, SalesDocument, SalesDocumentDelivery, SalesTemplate
from apps.sales.models_lifecycle import (
    SalesPaymentCheckout,
    SalesPaymentGateway,
)
from apps.sales.services import (
    SalesDeliveryError,
    calculate_line_items,
    default_email_body,
    default_email_subject,
    default_item_table_config,
    default_template_body,
    default_whatsapp_body,
    deliver_email,
    deliver_whatsapp,
    delivery_drafts,
    ensure_default_templates,
    next_document_number,
    normalize_item_table_config,
    public_url_for,
    refresh_whatsapp_delivery_statuses,
    sanitize_layout_html,
    snapshot_document_presentation,
    validate_brand_asset,
)


from .views_documents import _document_type, _organization


@crm_login_required
def sales_template_list_view(request):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can manage Sales templates."
        )
    organization = _organization(request)
    ensure_default_templates(organization=organization, user=request.crm_user)
    templates = SalesTemplate.objects.filter(
        organization=organization,
    ).order_by("document_type", "-is_default", "name")
    return render(
        request,
        "sales/template_list.html",
        {
            "templates": templates,
            "document_types": DocumentType,
        },
    )


@crm_login_required
def sales_template_form_view(request, template_id=None):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can manage Sales templates."
        )
    organization = _organization(request)
    template = None
    if template_id:
        template = get_object_or_404(
            SalesTemplate,
            id=template_id,
            organization=organization,
        )

    selected_type = (
        template.document_type
        if template
        else request.GET.get("type", DocumentType.QUOTATION)
    )
    if selected_type not in DocumentType.values:
        selected_type = DocumentType.QUOTATION

    if request.method == "POST":
        selected_type = _document_type(request.POST.get("document_type", ""))
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Template name is required.")
        else:
            if template is None:
                template = SalesTemplate(
                    organization=organization,
                    created_by=request.crm_user,
                )
            template.document_type = selected_type
            template.name = name
            template.number_prefix = (request.POST.get("number_prefix") or "").strip().upper()[:12]
            template.logo_url = (request.POST.get("logo_url") or "").strip()
            template.signature_url = (request.POST.get("signature_url") or "").strip()
            template.accent_color = (request.POST.get("accent_color") or "#0071e3").strip()
            template.header_text = (request.POST.get("header_text") or "").strip()
            template.body_template = sanitize_layout_html(
                request.POST.get("body_template") or default_template_body(selected_type)
            )
            template.footer_text = (request.POST.get("footer_text") or "").strip()
            template.item_table_config = normalize_item_table_config(
                {
                    "show_name": request.POST.get("item_show_name") == "on",
                    "show_description": request.POST.get("item_show_description") == "on",
                    "description_separate": (
                        request.POST.get("item_description_separate") == "on"
                    ),
                    "show_qty": request.POST.get("item_show_qty") == "on",
                    "show_rate": request.POST.get("item_show_rate") == "on",
                    "show_tax": request.POST.get("item_show_tax") == "on",
                    "show_amount": request.POST.get("item_show_amount") == "on",
                    "show_summary": request.POST.get("item_show_summary") == "on",
                    "labels": {
                        "name": request.POST.get("item_label_name"),
                        "description": request.POST.get("item_label_description"),
                        "qty": request.POST.get("item_label_qty"),
                        "rate": request.POST.get("item_label_rate"),
                        "tax": request.POST.get("item_label_tax"),
                        "amount": request.POST.get("item_label_amount"),
                        "subtotal": request.POST.get("item_label_subtotal"),
                        "tax_total": request.POST.get("item_label_tax_total"),
                        "discount": request.POST.get("item_label_discount"),
                        "total": request.POST.get("item_label_total"),
                    },
                }
            )
            template.email_subject_template = (
                request.POST.get("email_subject_template")
                or default_email_subject(selected_type)
            ).strip()
            template.email_body_template = (
                request.POST.get("email_body_template")
                or default_email_body(selected_type)
            ).strip()
            template.whatsapp_body_template = (
                request.POST.get("whatsapp_body_template")
                or default_whatsapp_body(selected_type)
            ).strip()
            template.is_active = request.POST.get("is_active") == "on"
            template.is_default = request.POST.get("is_default") == "on"

            try:
                logo_upload = validate_brand_asset(
                    request.FILES.get("logo_file"),
                    label="Logo",
                )
                signature_upload = validate_brand_asset(
                    request.FILES.get("signature_file"),
                    label="Signature",
                )
                if logo_upload is not None:
                    template.logo_file = logo_upload
                if signature_upload is not None:
                    template.signature_file = signature_upload
                if request.POST.get("remove_logo") == "on":
                    template.logo_file = ""
                    template.logo_url = ""
                if request.POST.get("remove_signature") == "on":
                    template.signature_file = ""
                    template.signature_url = ""
                template.full_clean()
            except ValidationError as exc:
                # Field/model validation usually exposes message_dict, while
                # upload validators raise a plain ValidationError with messages.
                # Never let the error-rendering path turn a user validation
                # problem into a Server Error (500).
                if hasattr(exc, "message_dict"):
                    error_groups = exc.message_dict.values()
                else:
                    error_groups = [getattr(exc, "messages", [str(exc)])]
                for errors in error_groups:
                    for error in errors:
                        messages.error(request, error)
            else:
                if template.is_default:
                    SalesTemplate.objects.filter(
                        organization=organization,
                        document_type=selected_type,
                    ).exclude(pk=template.pk).update(is_default=False)
                template.save()
                messages.success(request, f"Template “{template.name}” saved.")
                return redirect("shvya-sales-template-list")

    return render(
        request,
        "sales/template_form.html",
        {
            "template": template,
            "selected_type": selected_type,
            "document_types": DocumentType,
            "default_body": default_template_body(selected_type),
            "default_email_subject": default_email_subject(selected_type),
            "default_email_body": default_email_body(selected_type),
            "default_whatsapp_body": default_whatsapp_body(selected_type),
            "item_table_config": normalize_item_table_config(
                template.item_table_config
                if template
                else default_item_table_config()
            ),
        },
    )
