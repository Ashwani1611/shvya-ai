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


def _organization(request):
    user = request.crm_user
    if not user.organization_id:
        raise Http404
    return user.organization


def _document_type(value):
    if value not in DocumentType.values:
        raise Http404
    return value


def _clean_optional_email(value, *, label="Recipient email"):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        validate_email(value)
    except ValidationError as exc:
        raise ValidationError(f"{label} is invalid.") from exc
    return value


def _clean_date(value, *, label, required=False):
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValidationError(f"{label} is required.")
        return None
    try:
        parsed = parse_date(raw)
    except ValueError as exc:
        raise ValidationError(f"{label} is invalid.") from exc
    if parsed is None:
        raise ValidationError(f"{label} is invalid.")
    return parsed


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
            "sales_admin": is_sales_admin(request.crm_user),
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

    source_id = (
        request.POST.get("source_document_id", "").strip()
        if request.method == "POST"
        else request.GET.get("from", "").strip()
    )
    source_document = None
    if source_id:
        try:
            source_document = SalesDocument.objects.select_related("lead").get(
                id=source_id,
                organization=organization,
            )
        except (SalesDocument.DoesNotExist, ValidationError, ValueError):
            raise Http404 from None

        allowed_targets = {
            DocumentType.QUOTATION: {DocumentType.AGREEMENT, DocumentType.INVOICE},
            DocumentType.AGREEMENT: {DocumentType.INVOICE},
            DocumentType.INVOICE: set(),
        }
        if document_type not in allowed_targets.get(source_document.document_type, set()):
            raise Http404

    leads = Lead.objects.filter(
        organization=organization,
    ).select_related("pipeline", "stage").order_by("name")
    templates = _templates_for(organization, document_type)

    today = timezone.localdate()
    initial_items = list(source_document.line_items or []) if source_document else []
    initial_values = {
        "lead_id": str(source_document.lead_id) if source_document and source_document.lead_id else "",
        "title": (
            f"{DocumentType(document_type).label} for {source_document.recipient_name}"
            if source_document and source_document.recipient_name
            else ""
        ),
        "recipient_name": source_document.recipient_name if source_document else "",
        "recipient_email": source_document.recipient_email if source_document else "",
        "recipient_phone": source_document.recipient_phone if source_document else "",
        "currency": source_document.currency if source_document else "INR",
        "issue_date": today.isoformat(),
        "valid_until": "",
        "due_date": "",
        "content": source_document.content if source_document else "",
        "terms": source_document.terms if source_document else "",
        "layout_override": source_document.layout_override if source_document else "",
        "discount_total": str(source_document.discount_total) if source_document else "0",
    }

    if request.method == "POST":
        initial_values.update(
            {
                "lead_id": request.POST.get("lead_id", "").strip(),
                "title": request.POST.get("title", "").strip(),
                "recipient_name": request.POST.get("recipient_name", "").strip(),
                "recipient_email": request.POST.get("recipient_email", "").strip(),
                "recipient_phone": request.POST.get("recipient_phone", "").strip(),
                "currency": (request.POST.get("currency") or "INR").strip().upper()[:8],
                "issue_date": request.POST.get("issue_date", "").strip() or today.isoformat(),
                "valid_until": request.POST.get("valid_until", "").strip(),
                "due_date": request.POST.get("due_date", "").strip(),
                "content": request.POST.get("content", "").strip(),
                "terms": request.POST.get("terms", "").strip(),
                "layout_override": sanitize_layout_html(
                    request.POST.get("layout_override", "")
                ),
                "discount_total": request.POST.get("discount_total", "0").strip() or "0",
            }
        )

        lead = None
        lead_id = initial_values["lead_id"]
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
        initial_items = raw_items if isinstance(raw_items, list) else []

        try:
            financials = calculate_line_items(
                initial_items,
                discount_total=initial_values["discount_total"],
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            validation_error = ""
            try:
                issue_date = _clean_date(
                    initial_values["issue_date"],
                    label="Issue date",
                    required=True,
                )
                valid_until = _clean_date(
                    initial_values["valid_until"],
                    label="Valid/end date",
                )
                due_date = _clean_date(
                    initial_values["due_date"],
                    label="Due date",
                )
                initial_values["recipient_email"] = _clean_optional_email(
                    initial_values["recipient_email"]
                )
            except ValidationError as exc:
                validation_error = "; ".join(exc.messages)

            if not validation_error and valid_until and valid_until < issue_date:
                validation_error = "Valid/end date cannot be before the issue date."
            elif not validation_error and due_date and due_date < issue_date:
                validation_error = "Invoice due date cannot be before the issue date."

            if validation_error:
                messages.error(request, validation_error)
            else:
                recipient_name = initial_values["recipient_name"]
                recipient_email = initial_values["recipient_email"]
                recipient_phone = initial_values["recipient_phone"]

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
                    source_document=source_document,
                    lead=lead,
                    created_by=request.crm_user,
                    document_number=document_number,
                    title=initial_values["title"]
                    or f"{DocumentType(document_type).label} {document_number}",
                    recipient_name=recipient_name,
                    recipient_email=recipient_email,
                    recipient_phone=recipient_phone,
                    currency=initial_values["currency"],
                    issue_date=issue_date,
                    valid_until=valid_until,
                    due_date=due_date,
                    line_items=financials["items"],
                    subtotal=financials["subtotal"],
                    tax_total=financials["tax_total"],
                    discount_total=financials["discount_total"],
                    total=financials["total"],
                    content=initial_values["content"],
                    terms=initial_values["terms"],
                    layout_override=initial_values["layout_override"],
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
                record_activity(
                    document,
                    event_type="document_created",
                    message=f"{document.get_document_type_display()} {document.document_number} created.",
                    actor=request.crm_user,
                    metadata={
                        "document_type": document.document_type,
                        "source_document_id": (
                            str(source_document.id) if source_document else ""
                        ),
                    },
                )
                messages.success(
                    request,
                    f"{document.get_document_type_display()} {document.document_number} created.",
                )
                return redirect("shvya-sales-document-detail", document_id=document.id)

    return render(
        request,
        "sales/document_form.html",
        {
            "document_type": document_type,
            "document_type_label": DocumentType(document_type).label,
            "leads": leads[:1000],
            "templates": templates,
            "today": today,
            "source_document": source_document,
            "initial_line_items": initial_items,
            "initial": initial_values,
            "selected_template_id": (
                request.POST.get("template_id", "").strip()
                if request.method == "POST"
                else ""
            ),
        },
    )


@crm_login_required
def sales_document_edit_view(request, document_id):
    organization = _organization(request)
    document = get_object_or_404(
        SalesDocument.objects.select_related("lead", "template", "source_document"),
        id=document_id,
        organization=organization,
        status=SalesDocument.Status.DRAFT,
    )
    if document.scheduled_deliveries.filter(status="pending").exists():
        messages.error(
            request,
            "Cancel the pending scheduled send before editing this draft.",
        )
        return redirect("shvya-sales-document-detail", document_id=document.id)

    document_type = document.document_type
    ensure_default_templates(organization=organization, user=request.crm_user)

    leads = Lead.objects.filter(
        organization=organization,
    ).select_related("pipeline", "stage").order_by("name")
    templates = _templates_for(organization, document_type)
    today = timezone.localdate()

    initial_items = list(document.line_items or [])
    initial_values = {
        "lead_id": str(document.lead_id) if document.lead_id else "",
        "title": document.title,
        "recipient_name": document.recipient_name,
        "recipient_email": document.recipient_email,
        "recipient_phone": document.recipient_phone,
        "currency": document.currency,
        "issue_date": document.issue_date.isoformat(),
        "valid_until": document.valid_until.isoformat() if document.valid_until else "",
        "due_date": document.due_date.isoformat() if document.due_date else "",
        "content": document.content,
        "terms": document.terms,
        "layout_override": document.layout_override,
        "discount_total": str(document.discount_total),
    }
    selected_template_id = str(document.template_id) if document.template_id else ""

    if request.method == "POST":
        initial_values.update(
            {
                "lead_id": request.POST.get("lead_id", "").strip(),
                "title": request.POST.get("title", "").strip(),
                "recipient_name": request.POST.get("recipient_name", "").strip(),
                "recipient_email": request.POST.get("recipient_email", "").strip(),
                "recipient_phone": request.POST.get("recipient_phone", "").strip(),
                "currency": (request.POST.get("currency") or "INR").strip().upper()[:8],
                "issue_date": request.POST.get("issue_date", "").strip() or today.isoformat(),
                "valid_until": request.POST.get("valid_until", "").strip(),
                "due_date": request.POST.get("due_date", "").strip(),
                "content": request.POST.get("content", "").strip(),
                "terms": request.POST.get("terms", "").strip(),
                "layout_override": sanitize_layout_html(
                    request.POST.get("layout_override", "")
                ),
                "discount_total": request.POST.get("discount_total", "0").strip() or "0",
            }
        )
        selected_template_id = request.POST.get("template_id", "").strip()

        lead = None
        if initial_values["lead_id"]:
            lead = get_object_or_404(
                Lead.objects.select_related("pipeline", "stage"),
                id=initial_values["lead_id"],
                organization=organization,
            )

        template = None
        if selected_template_id:
            template = get_object_or_404(
                SalesTemplate,
                id=selected_template_id,
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
        initial_items = raw_items if isinstance(raw_items, list) else []

        try:
            financials = calculate_line_items(
                initial_items,
                discount_total=initial_values["discount_total"],
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            validation_error = ""
            try:
                issue_date = _clean_date(
                    initial_values["issue_date"],
                    label="Issue date",
                    required=True,
                )
                valid_until = _clean_date(
                    initial_values["valid_until"],
                    label="Valid/end date",
                )
                due_date = _clean_date(
                    initial_values["due_date"],
                    label="Due date",
                )
                initial_values["recipient_email"] = _clean_optional_email(
                    initial_values["recipient_email"]
                )
            except ValidationError as exc:
                validation_error = "; ".join(exc.messages)

            if not validation_error and valid_until and valid_until < issue_date:
                validation_error = "Valid/end date cannot be before the issue date."
            elif not validation_error and due_date and due_date < issue_date:
                validation_error = "Invoice due date cannot be before the issue date."

            if validation_error:
                messages.error(request, validation_error)
            else:
                recipient_name = initial_values["recipient_name"]
                recipient_email = initial_values["recipient_email"]
                recipient_phone = initial_values["recipient_phone"]
                if lead:
                    recipient_name = recipient_name or lead.name
                    recipient_email = recipient_email or lead.email
                    recipient_phone = recipient_phone or lead.phone

                document.template = template
                document.lead = lead
                document.title = initial_values["title"] or (
                    f"{document.get_document_type_display()} {document.document_number}"
                )
                document.recipient_name = recipient_name
                document.recipient_email = recipient_email
                document.recipient_phone = recipient_phone
                document.currency = initial_values["currency"]
                document.issue_date = issue_date
                document.valid_until = valid_until
                document.due_date = due_date
                document.line_items = financials["items"]
                document.subtotal = financials["subtotal"]
                document.tax_total = financials["tax_total"]
                document.discount_total = financials["discount_total"]
                document.total = financials["total"]
                document.content = initial_values["content"]
                document.terms = initial_values["terms"]
                document.layout_override = initial_values["layout_override"]
                invalidate_draft_artifacts(document)
                snapshot_document_presentation(
                    document,
                    public_url=reverse(
                        "shvya-sales-public-document",
                        args=[document.public_token],
                    ),
                )
                document.save()
                record_activity(
                    document,
                    event_type="document_edited",
                    message=f"Draft {document.document_number} updated.",
                    actor=request.crm_user,
                )
                messages.success(
                    request,
                    f"Draft {document.document_number} updated.",
                )
                return redirect("shvya-sales-document-detail", document_id=document.id)

    return render(
        request,
        "sales/document_form.html",
        {
            "document": document,
            "document_type": document_type,
            "document_type_label": document.get_document_type_display(),
            "leads": leads[:1000],
            "templates": templates,
            "today": today,
            "source_document": document.source_document,
            "initial_line_items": initial_items,
            "initial": initial_values,
            "selected_template_id": selected_template_id,
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
            "source_document",
        ),
        id=document_id,
        organization=organization,
    )
    public_url = public_url_for(document, request)
    drafts = delivery_drafts(document, public_url=public_url)
    refresh_whatsapp_delivery_statuses(document)

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

    whatsapp_templates = WhatsAppTemplate.objects.none()
    if (
        whatsapp_account
        and whatsapp_account.connection_type == WhatsAppAccount.ConnectionType.API
    ):
        whatsapp_templates = (
            WhatsAppTemplate.objects.filter(
                organization=organization,
                account=whatsapp_account,
                status=WhatsAppTemplate.Status.APPROVED,
                attachment_type=WhatsAppTemplate.AttachmentType.DOCUMENT,
            )
            .exclude(meta_template_id="")
            .order_by("name")
        )

    has_whatsapp_templates = whatsapp_templates.exists()
    whatsapp_send_ready = bool(whatsapp_account)
    if (
        whatsapp_account
        and whatsapp_account.connection_type == WhatsAppAccount.ConnectionType.API
        and not whatsapp_window_open
        and not has_whatsapp_templates
    ):
        whatsapp_send_ready = False

    ledger = invoice_ledger(document) if document.document_type == DocumentType.INVOICE else None
    gateways = (
        SalesPaymentGateway.objects.filter(
            organization=organization,
            is_enabled=True,
        ).order_by("provider")
        if document.document_type == DocumentType.INVOICE
        else SalesPaymentGateway.objects.none()
    )
    active_checkout = (
        document.payment_checkouts.filter(
            status=SalesPaymentCheckout.Status.CREATED,
        ).first()
        if document.document_type == DocumentType.INVOICE
        else None
    )
    recurring_rule = None
    if document.document_type == DocumentType.INVOICE:
        try:
            recurring_rule = document.recurring_rule
        except Exception:
            recurring_rule = None

    return render(
        request,
        "sales/document_detail.html",
        {
            "document": document,
            "deliveries": document.deliveries.all()[:30],
            "public_url": public_url,
            "drafts": drafts,
            "email_configuration": email_configuration,
            "whatsapp_account": whatsapp_account,
            "whatsapp_error": whatsapp_error,
            "whatsapp_window_open": whatsapp_window_open,
            "whatsapp_templates": whatsapp_templates,
            "has_whatsapp_templates": has_whatsapp_templates,
            "whatsapp_send_ready": whatsapp_send_ready,
            "attachments": document.attachments.all()[:30],
            "activities": document.activities.all()[:50],
            "scheduled_deliveries": document.scheduled_deliveries.all()[:20],
            "invoice_ledger": ledger,
            "payment_gateways": gateways,
            "active_checkout": active_checkout,
            "recurring_rule": recurring_rule,
            "sales_admin": is_sales_admin(request.crm_user),
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

    base_url = request.build_absolute_uri("/")
    email_subject = request.POST.get("email_subject", "")
    email_body = request.POST.get("email_body", "")
    whatsapp_body = request.POST.get("whatsapp_body", "")
    whatsapp_template_id = str(
        request.POST.get("whatsapp_template_id") or ""
    ).strip()
    send_mode = str(request.POST.get("send_mode") or "now")

    if send_mode == "schedule":
        raw = str(request.POST.get("scheduled_at") or "").strip()
        scheduled_at = parse_datetime(raw)
        if scheduled_at is None:
            try:
                scheduled_at = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                scheduled_at = None
        if scheduled_at is not None and timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(
                scheduled_at,
                timezone.get_current_timezone(),
            )
        if scheduled_at is None:
            messages.error(request, "Choose a valid future schedule time.")
            return redirect("shvya-sales-document-detail", document_id=document.id)
        whatsapp_template = None
        if SalesDocumentDelivery.Channel.WHATSAPP in channels:
            if not document.lead_id:
                messages.error(
                    request,
                    "Link this document to a CRM lead before scheduling WhatsApp.",
                )
                return redirect(
                    "shvya-sales-document-detail",
                    document_id=document.id,
                )
            from services.crm.lead_chat import pipeline_chat_account

            try:
                scheduled_account = pipeline_chat_account(document.lead)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
                return redirect(
                    "shvya-sales-document-detail",
                    document_id=document.id,
                )
            if scheduled_account.connection_type == WhatsAppAccount.ConnectionType.API:
                whatsapp_template = (
                    WhatsAppTemplate.objects.filter(
                        id=whatsapp_template_id,
                        organization=organization,
                        account=scheduled_account,
                        status=WhatsAppTemplate.Status.APPROVED,
                        attachment_type=WhatsAppTemplate.AttachmentType.DOCUMENT,
                    )
                    .exclude(meta_template_id="")
                    .first()
                )
                if whatsapp_template is None:
                    messages.error(
                        request,
                        "Scheduled Cloud API/Coexistence sends require an "
                        "approved document-header template for this pipeline number.",
                    )
                    return redirect(
                        "shvya-sales-document-detail",
                        document_id=document.id,
                    )

        try:
            schedule = create_scheduled_delivery(
                document=document,
                channels=list(channels),
                scheduled_at=scheduled_at,
                email_subject=email_subject,
                email_body=email_body,
                whatsapp_body=whatsapp_body,
                whatsapp_template=whatsapp_template,
                base_url=base_url,
                actor=request.crm_user,
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(
                request,
                f"Scheduled for {schedule.scheduled_at:%d %b %Y, %I:%M %p}.",
            )
        return redirect("shvya-sales-document-detail", document_id=document.id)

    successes = []
    failures = []
    if SalesDocumentDelivery.Channel.EMAIL in channels:
        try:
            deliver_email(
                document=document,
                user=request.crm_user,
                subject=email_subject,
                body=email_body,
                base_url=base_url,
                attach_pdf=True,
            )
            successes.append("Email")
        except SalesDeliveryError as exc:
            failures.append(f"Email: {exc}")

    if SalesDocumentDelivery.Channel.WHATSAPP in channels:
        try:
            deliver_whatsapp(
                document=document,
                user=request.crm_user,
                body=whatsapp_body,
                base_url=base_url,
                attach_pdf=True,
                whatsapp_template_id=whatsapp_template_id or None,
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
