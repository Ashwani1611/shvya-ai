import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from apps.channels.models import WhatsAppAccount
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.integrations.models import EmailConfiguration
from apps.sales.models import DocumentType, SalesDocument, SalesDocumentDelivery, SalesTemplate
from apps.sales.services import (
    SalesDeliveryError,
    calculate_line_items,
    default_email_body,
    default_email_subject,
    default_template_body,
    default_whatsapp_body,
    deliver_email,
    deliver_whatsapp,
    delivery_drafts,
    ensure_default_templates,
    next_document_number,
    public_url_for,
    sanitize_layout_html,
    snapshot_document_presentation,
)


def _organization(request):
    user = request.crm_user
    if not user.organization_id:
        raise Http404
    return user.organization


def _document_type(value):
    if value not in DocumentType.values:
        raise Http404
    return value


def _templates_for(organization, document_type=None):
    queryset = SalesTemplate.objects.filter(
        organization=organization,
        is_active=True,
    )
    if document_type:
        queryset = queryset.filter(document_type=document_type)
    return queryset.order_by("-is_default", "name")


@crm_login_required
def sales_dashboard_view(request):
    organization = _organization(request)
    ensure_default_templates(organization=organization, user=request.crm_user)

    documents = SalesDocument.objects.filter(organization=organization)
    counts = {
        item["document_type"]: item["count"]
        for item in documents.values("document_type").annotate(count=Count("id"))
    }
    open_value = (
        documents.exclude(
            status__in=[
                SalesDocument.Status.PAID,
                SalesDocument.Status.CANCELLED,
                SalesDocument.Status.DECLINED,
            ]
        )
        .filter(document_type__in=[DocumentType.QUOTATION, DocumentType.INVOICE])
    )
    recent = documents.select_related("lead", "template")[:8]
    recent_deliveries = SalesDocumentDelivery.objects.filter(
        organization=organization,
    ).select_related("document")[:5]

    email_connected = EmailConfiguration.objects.filter(
        organization=organization,
        is_enabled=True,
        last_test_status=EmailConfiguration.TestStatus.SUCCESS,
    ).exists()
    whatsapp_connected = WhatsAppAccount.objects.filter(
        organization=organization,
        is_active=True,
        status=WhatsAppAccount.Status.CONNECTED,
    ).count()

    return render(
        request,
        "sales/dashboard.html",
        {
            "counts": counts,
            "quotation_count": counts.get(DocumentType.QUOTATION, 0),
            "agreement_count": counts.get(DocumentType.AGREEMENT, 0),
            "invoice_count": counts.get(DocumentType.INVOICE, 0),
            "open_value": sum((document.total for document in open_value), start=0),
            "recent_documents": recent,
            "recent_deliveries": recent_deliveries,
            "email_connected": email_connected,
            "whatsapp_connected": whatsapp_connected,
        },
    )


@crm_login_required
def sales_document_list_view(request):
    organization = _organization(request)
    selected_type = request.GET.get("type", "").strip()
    if selected_type:
        _document_type(selected_type)

    documents = SalesDocument.objects.filter(
        organization=organization,
    ).select_related("lead", "template")
    if selected_type:
        documents = documents.filter(document_type=selected_type)

    query = request.GET.get("q", "").strip()
    if query:
        documents = documents.filter(
            Q(document_number__icontains=query)
            | Q(title__icontains=query)
            | Q(recipient_name__icontains=query)
            | Q(recipient_email__icontains=query)
        )

    return render(
        request,
        "sales/document_list.html",
        {
            "documents": documents[:250],
            "selected_type": selected_type,
            "search_query": query,
            "document_types": DocumentType,
        },
    )


@crm_login_required
def sales_document_create_view(request, document_type):
    organization = _organization(request)
    document_type = _document_type(document_type)
    ensure_default_templates(organization=organization, user=request.crm_user)

    leads = Lead.objects.filter(
        organization=organization,
    ).select_related("pipeline", "stage").order_by("name")
    templates = _templates_for(organization, document_type)

    if request.method == "POST":
        lead = None
        lead_id = request.POST.get("lead_id", "").strip()
        if lead_id:
            lead = get_object_or_404(
                Lead.objects.select_related("pipeline", "stage"),
                id=lead_id,
                organization=organization,
            )

        template = None
        template_id = request.POST.get("template_id", "").strip()
        if template_id:
            template = get_object_or_404(
                SalesTemplate,
                id=template_id,
                organization=organization,
                document_type=document_type,
                is_active=True,
            )
        if template is None:
            template = templates.first()

        try:
            raw_items = json.loads(request.POST.get("line_items_json") or "[]")
        except json.JSONDecodeError:
            raw_items = []

        try:
            financials = calculate_line_items(
                raw_items,
                discount_total=request.POST.get("discount_total", "0"),
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            issue_date = parse_date(request.POST.get("issue_date", "")) or timezone.localdate()
            valid_until = parse_date(request.POST.get("valid_until", "")) or None
            due_date = parse_date(request.POST.get("due_date", "")) or None
            recipient_name = (request.POST.get("recipient_name") or "").strip()
            recipient_email = (request.POST.get("recipient_email") or "").strip()
            recipient_phone = (request.POST.get("recipient_phone") or "").strip()

            if lead:
                recipient_name = recipient_name or lead.name
                recipient_email = recipient_email or lead.email
                recipient_phone = recipient_phone or lead.phone

            document_number = next_document_number(
                organization=organization,
                document_type=document_type,
                prefix=(template.number_prefix if template else ""),
            )
            document = SalesDocument.objects.create(
                organization=organization,
                document_type=document_type,
                template=template,
                lead=lead,
                created_by=request.crm_user,
                document_number=document_number,
                title=(request.POST.get("title") or "").strip()
                or f"{DocumentType(document_type).label} {document_number}",
                recipient_name=recipient_name,
                recipient_email=recipient_email,
                recipient_phone=recipient_phone,
                currency=(request.POST.get("currency") or "INR").strip().upper()[:8],
                issue_date=issue_date,
                valid_until=valid_until,
                due_date=due_date,
                line_items=financials["items"],
                subtotal=financials["subtotal"],
                tax_total=financials["tax_total"],
                discount_total=financials["discount_total"],
                total=financials["total"],
                content=(request.POST.get("content") or "").strip(),
                terms=(request.POST.get("terms") or "").strip(),
            )
            snapshot_document_presentation(
                document,
                public_url=reverse(
                    "shvya-sales-public-document",
                    args=[document.public_token],
                ),
            )
            document.save(
                update_fields=[
                    "presentation_snapshot",
                    "rendered_html",
                    "email_subject_snapshot",
                    "email_body_snapshot",
                    "whatsapp_body_snapshot",
                    "updated_at",
                ]
            )
            messages.success(request, f"{document.get_document_type_display()} {document.document_number} created.")
            return redirect("shvya-sales-document-detail", document_id=document.id)

    return render(
        request,
        "sales/document_form.html",
        {
            "document_type": document_type,
            "document_type_label": DocumentType(document_type).label,
            "leads": leads[:1000],
            "templates": templates,
            "today": timezone.localdate(),
        },
    )


@crm_login_required
def sales_document_detail_view(request, document_id):
    organization = _organization(request)
    document = get_object_or_404(
        SalesDocument.objects.select_related(
            "lead",
            "lead__pipeline",
            "lead__stage",
            "template",
        ),
        id=document_id,
        organization=organization,
    )
    public_url = public_url_for(document, request)
    drafts = delivery_drafts(document, public_url=public_url)

    email_configuration = EmailConfiguration.objects.filter(
        organization=organization,
        is_enabled=True,
        last_test_status=EmailConfiguration.TestStatus.SUCCESS,
    ).first()

    whatsapp_account = None
    whatsapp_error = ""
    whatsapp_window_open = True
    if document.lead_id:
        try:
            from services.crm.lead_chat import pipeline_chat_account

            whatsapp_account = pipeline_chat_account(document.lead)
            if whatsapp_account.connection_type == WhatsAppAccount.ConnectionType.API:
                from services.channels.whatsapp_api_chat_service import is_within_api_24h_window

                whatsapp_window_open = is_within_api_24h_window(
                    lead=document.lead,
                    account=whatsapp_account,
                )
        except ValidationError as exc:
            whatsapp_error = "; ".join(getattr(exc, "messages", None) or [str(exc)])

    return render(
        request,
        "sales/document_detail.html",
        {
            "document": document,
            "deliveries": document.deliveries.all()[:20],
            "public_url": public_url,
            "drafts": drafts,
            "email_configuration": email_configuration,
            "whatsapp_account": whatsapp_account,
            "whatsapp_error": whatsapp_error,
            "whatsapp_window_open": whatsapp_window_open,
        },
    )


@crm_login_required
@require_POST
def sales_document_send_view(request, document_id):
    organization = _organization(request)
    document = get_object_or_404(
        SalesDocument.objects.select_related("lead", "lead__pipeline"),
        id=document_id,
        organization=organization,
    )
    channels = set(request.POST.getlist("channels"))
    allowed = {
        SalesDocumentDelivery.Channel.EMAIL,
        SalesDocumentDelivery.Channel.WHATSAPP,
    }
    channels &= allowed
    if not channels:
        messages.error(request, "Select Email, WhatsApp, or both.")
        return redirect("shvya-sales-document-detail", document_id=document.id)

    successes = []
    failures = []

    if SalesDocumentDelivery.Channel.EMAIL in channels:
        try:
            deliver_email(
                document=document,
                user=request.crm_user,
                subject=request.POST.get("email_subject", ""),
                body=request.POST.get("email_body", ""),
            )
            successes.append("Email")
        except SalesDeliveryError as exc:
            failures.append(f"Email: {exc}")

    if SalesDocumentDelivery.Channel.WHATSAPP in channels:
        try:
            deliver_whatsapp(
                document=document,
                user=request.crm_user,
                body=request.POST.get("whatsapp_body", ""),
            )
            successes.append("WhatsApp")
        except SalesDeliveryError as exc:
            failures.append(f"WhatsApp: {exc}")

    if successes:
        messages.success(request, f"Queued/sent successfully via {', '.join(successes)}.")
    for failure in failures:
        messages.error(request, failure)

    return redirect("shvya-sales-document-detail", document_id=document.id)


@crm_login_required
def sales_template_list_view(request):
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
                template.full_clean()
            except ValidationError as exc:
                for errors in exc.message_dict.values():
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
        },
    )


def public_document_view(request, token):
    document = get_object_or_404(
        SalesDocument.objects.select_related("organization", "template"),
        public_token=token,
    )
    if document.status == SalesDocument.Status.DRAFT:
        raise Http404

    return render(
        request,
        "sales/public_document.html",
        {
            "document": document,
            "presentation": document.presentation_snapshot or {},
        },
    )


@require_POST
def public_document_action_view(request, token):
    document = get_object_or_404(SalesDocument, public_token=token)
    if document.status == SalesDocument.Status.DRAFT:
        raise Http404

    action = (request.POST.get("action") or "").strip()
    now = timezone.now()
    name = (request.POST.get("name") or "").strip()
    email = (request.POST.get("email") or "").strip()
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
    return redirect("shvya-sales-public-document", token=token)
