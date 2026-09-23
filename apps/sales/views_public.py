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


from .views_documents import _clean_optional_email


def public_document_view(request, token):
    document = get_object_or_404(
        SalesDocument.objects.select_related("organization", "template"),
        public_token=token,
    )
    if document.status == SalesDocument.Status.DRAFT:
        raise Http404

    now = timezone.now()
    first_view = document.first_viewed_at is None
    updates = {
        "view_count": F("view_count") + 1,
        "last_viewed_at": now,
    }
    if first_view:
        updates["first_viewed_at"] = now
    SalesDocument.objects.filter(pk=document.pk).update(**updates)
    document.refresh_from_db(
        fields=["view_count", "first_viewed_at", "last_viewed_at"]
    )
    if first_view:
        record_activity(
            document,
            event_type="document_viewed",
            message=f"{document.document_number} was viewed by the customer.",
            metadata={"view_count": document.view_count},
        )

    ledger = invoice_ledger(document) if document.document_type == DocumentType.INVOICE else None
    active_checkout = (
        document.payment_checkouts.filter(
            status=SalesPaymentCheckout.Status.CREATED,
        ).first()
        if document.document_type == DocumentType.INVOICE
        else None
    )
    return render(
        request,
        "sales/public_document.html",
        {
            "document": document,
            "presentation": document.presentation_snapshot or {},
            "customer_attachments": document.attachments.filter(
                visible_to_customer=True
            ),
            "invoice_ledger": ledger,
            "active_checkout": active_checkout,
        },
    )


@require_POST
@transaction.atomic
def public_document_action_view(request, token):
    document = get_object_or_404(
        SalesDocument.objects.select_for_update(),
        public_token=token,
    )
    if document.status == SalesDocument.Status.DRAFT:
        raise Http404

    action = (request.POST.get("action") or "").strip()
    now = timezone.now()
    name = (request.POST.get("name") or "").strip()
    email = (request.POST.get("email") or "").strip()
    try:
        email = _clean_optional_email(email, label="Acceptance email")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("shvya-sales-public-document", token=token)
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    client_ip = forwarded_for.split(",", 1)[0].strip() if forwarded_for else request.META.get("REMOTE_ADDR")

    if document.document_type == DocumentType.QUOTATION:
        if document.status in {
            SalesDocument.Status.ACCEPTED,
            SalesDocument.Status.DECLINED,
        }:
            return redirect("shvya-sales-public-document", token=token)
        if document.status != SalesDocument.Status.SENT:
            raise Http404
        if action == "accept":
            document.status = SalesDocument.Status.ACCEPTED
            document.accepted_at = now
        elif action == "decline":
            document.status = SalesDocument.Status.DECLINED
        else:
            raise Http404
    elif document.document_type == DocumentType.AGREEMENT and action == "sign":
        if document.status == SalesDocument.Status.SIGNED:
            return redirect("shvya-sales-public-document", token=token)
        if document.status != SalesDocument.Status.SENT:
            raise Http404
        if not name:
            messages.error(request, "Enter your full name to sign the agreement.")
            return redirect("shvya-sales-public-document", token=token)
        document.status = SalesDocument.Status.SIGNED
        document.signed_at = now
        document.accepted_at = now
    else:
        raise Http404

    document.accepted_by_name = name
    document.accepted_by_email = email
    document.accepted_ip = client_ip or None
    document.save(
        update_fields=[
            "status",
            "accepted_at",
            "signed_at",
            "accepted_by_name",
            "accepted_by_email",
            "accepted_ip",
            "updated_at",
        ]
    )
    record_activity(
        document,
        event_type=(
            "agreement_signed"
            if document.document_type == DocumentType.AGREEMENT
            else (
                "quotation_accepted"
                if document.status == SalesDocument.Status.ACCEPTED
                else "quotation_declined"
            )
        ),
        message=(
            f"Agreement {document.document_number} signed."
            if document.document_type == DocumentType.AGREEMENT
            else f"Quotation {document.document_number} {document.status}."
        ),
        metadata={
            "signer_name": document.accepted_by_name,
            "signer_email": document.accepted_by_email,
        },
    )
    if document.status in {
        SalesDocument.Status.ACCEPTED,
        SalesDocument.Status.SIGNED,
    }:
        from apps.sales.pdf_service import SalesPDFError, ensure_document_pdf

        try:
            ensure_document_pdf(document, force=True)
        except SalesPDFError:
            record_activity(
                document,
                event_type="pdf_finalize_failed",
                message="Final accepted/signed PDF could not be regenerated.",
            )
    return redirect("shvya-sales-public-document", token=token)
