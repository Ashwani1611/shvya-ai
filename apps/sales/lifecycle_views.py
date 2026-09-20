from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from apps.sales.access import is_sales_admin
from apps.sales.activity import record_activity
from apps.sales.lifecycle import (
    add_attachment,
    apply_credit_note,
    create_agreement_revision,
    create_credit_note,
    disable_recurring_rule,
    record_payment,
    record_refund,
)
from apps.sales.models import DocumentType, SalesDocument, SalesDocumentDelivery
from apps.sales.models_lifecycle import (
    SalesAttachment,
    SalesCreditNote,
    SalesPayment,
    SalesPaymentGateway,
    SalesRecurringInvoice,
    SalesScheduledDelivery,
    SalesSettings,
)
from apps.sales.payments import (
    SalesGatewayError,
    apply_razorpay_event,
    apply_stripe_event,
    create_payment_checkout,
    refund_gateway_payment,
    verify_razorpay_webhook,
    verify_stripe_webhook,
)
from apps.sales.pdf_service import SalesPDFError, ensure_document_pdf
from apps.sales.tracking import (
    record_email_click,
    record_email_open,
    record_provider_email_event,
)


_TRANSPARENT_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


def _organization(request):
    user = request.crm_user
    if not user.organization_id:
        raise Http404
    return user.organization


def _document(request, document_id):
    return get_object_or_404(
        SalesDocument.objects.select_related("organization", "lead", "template"),
        pk=document_id,
        organization=_organization(request),
    )


@crm_login_required
@require_GET
def sales_document_pdf_view(request, document_id):
    document = _document(request, document_id)
    try:
        ensure_document_pdf(
            document,
            actor=request.crm_user,
            force=(document.status == SalesDocument.Status.DRAFT),
        )
    except SalesPDFError as exc:
        messages.error(request, str(exc))
        return redirect("shvya-sales-document-detail", document_id=document.id)

    record_activity(
        document,
        event_type="pdf_downloaded",
        message=f"PDF downloaded for {document.document_number}.",
        actor=request.crm_user,
    )
    document.pdf_file.open("rb")
    return FileResponse(
        document.pdf_file,
        as_attachment=True,
        filename=f"{document.document_number}.pdf",
        content_type="application/pdf",
    )


@require_GET
def sales_public_pdf_view(request, token):
    document = get_object_or_404(
        SalesDocument.objects.select_related("organization", "template"),
        public_token=token,
    )
    if document.status == SalesDocument.Status.DRAFT:
        raise Http404
    try:
        ensure_document_pdf(document)
    except SalesPDFError:
        raise Http404 from None
    document.pdf_file.open("rb")
    response = FileResponse(
        document.pdf_file,
        as_attachment=True,
        filename=f"{document.document_number}.pdf",
        content_type="application/pdf",
    )
    response["Cache-Control"] = "private, max-age=300"
    return response


@require_GET
def sales_email_open_view(request, tracking_token):
    record_email_open(tracking_token=tracking_token)
    response = HttpResponse(_TRANSPARENT_GIF, content_type="image/gif")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


@require_GET
def sales_email_click_view(request, token):
    link = record_email_click(token=token)
    if link is None:
        raise Http404
    return redirect(link.target_url)


@csrf_exempt
@require_POST
def sales_email_provider_event_view(request):
    """Generic HMAC bridge for SMTP providers that can report delivered/bounce."""
    secret = str(getattr(settings, "SALES_EMAIL_EVENT_WEBHOOK_SECRET", "") or "")
    signature = str(request.headers.get("X-SHVYA-Sales-Signature") or "")
    if not secret or not signature:
        return HttpResponseForbidden("Email event webhook is not configured.")
    expected = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return HttpResponseForbidden("Invalid signature.")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("Invalid JSON.")

    delivery = None
    delivery_id = str(payload.get("delivery_id") or "")
    message_id = str(payload.get("message_id") or "")
    queryset = SalesDocumentDelivery.objects.filter(
        channel=SalesDocumentDelivery.Channel.EMAIL
    )
    if delivery_id:
        delivery = queryset.filter(pk=delivery_id).first()
    if delivery is None and message_id:
        delivery = queryset.filter(provider_message_id=message_id).first()
    if delivery is None:
        return JsonResponse({"status": "ignored", "reason": "delivery_not_found"})

    record_provider_email_event(
        delivery=delivery,
        event=payload.get("event"),
        reason=payload.get("reason") or payload.get("error") or "",
    )
    return JsonResponse({"status": "accepted"})


@crm_login_required
@require_POST
def sales_attachment_add_view(request, document_id):
    document = _document(request, document_id)
    try:
        add_attachment(
            document=document,
            uploaded_file=request.FILES.get("file"),
            actor=request.crm_user,
            visible_to_customer=request.POST.get("visible_to_customer") == "on",
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "Attachment added.")
    return redirect("shvya-sales-document-detail", document_id=document.id)


@crm_login_required
@require_GET
def sales_attachment_download_view(request, attachment_id):
    attachment = get_object_or_404(
        SalesAttachment,
        pk=attachment_id,
        organization=_organization(request),
    )
    attachment.file.open("rb")
    return FileResponse(
        attachment.file,
        as_attachment=True,
        filename=attachment.original_name,
        content_type=attachment.mime_type or "application/octet-stream",
    )


@require_GET
def sales_public_attachment_view(request, token, attachment_id):
    document = get_object_or_404(SalesDocument, public_token=token)
    if document.status == SalesDocument.Status.DRAFT:
        raise Http404
    attachment = get_object_or_404(
        SalesAttachment,
        pk=attachment_id,
        document=document,
        organization=document.organization,
        visible_to_customer=True,
    )
    attachment.file.open("rb")
    return FileResponse(
        attachment.file,
        as_attachment=True,
        filename=attachment.original_name,
        content_type=attachment.mime_type or "application/octet-stream",
    )


@crm_login_required
@require_POST
def sales_attachment_delete_view(request, attachment_id):
    attachment = get_object_or_404(
        SalesAttachment.objects.select_related("document"),
        pk=attachment_id,
        organization=_organization(request),
    )
    document = attachment.document
    filename = attachment.original_name
    try:
        attachment.file.delete(save=False)
    finally:
        attachment.delete()
    record_activity(
        document,
        event_type="attachment_deleted",
        message=f"Attachment {filename} deleted.",
        actor=request.crm_user,
    )
    messages.success(request, "Attachment deleted.")
    return redirect("shvya-sales-document-detail", document_id=document.id)


@crm_login_required
@require_POST
def sales_agreement_revision_view(request, document_id):
    agreement = _document(request, document_id)
    kind = str(request.POST.get("kind") or "")
    try:
        new_document = create_agreement_revision(
            agreement=agreement,
            kind=kind,
            actor=request.crm_user,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("shvya-sales-document-detail", document_id=agreement.id)
    messages.success(
        request,
        f"{kind.title()} draft {new_document.document_number} created.",
    )
    return redirect("shvya-sales-document-edit", document_id=new_document.id)


@crm_login_required
@require_POST
def sales_manual_payment_view(request, document_id):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can perform this Sales finance/configuration action."
        )
    invoice = _document(request, document_id)
    try:
        record_payment(
            invoice=invoice,
            amount=request.POST.get("amount"),
            method=request.POST.get("method") or SalesPayment.Method.OTHER,
            actor=request.crm_user,
            reference_number=request.POST.get("reference_number", ""),
            note=request.POST.get("note", ""),
        )
    except (ValidationError, ValueError) as exc:
        text = "; ".join(getattr(exc, "messages", None) or [str(exc)])
        messages.error(request, text)
    else:
        messages.success(request, "Payment recorded.")
    return redirect("shvya-sales-document-detail", document_id=invoice.id)


@crm_login_required
@require_POST
def sales_manual_refund_view(request, document_id):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can perform this Sales finance/configuration action."
        )
    invoice = _document(request, document_id)
    try:
        record_refund(
            invoice=invoice,
            amount=request.POST.get("amount"),
            actor=request.crm_user,
            reference_number=request.POST.get("reference_number", ""),
            note=request.POST.get("note", ""),
        )
    except (ValidationError, ValueError) as exc:
        text = "; ".join(getattr(exc, "messages", None) or [str(exc)])
        messages.error(request, text)
    else:
        messages.success(request, "Refund recorded.")
    return redirect("shvya-sales-document-detail", document_id=invoice.id)


@crm_login_required
@require_POST
def sales_gateway_refund_view(request, payment_id):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can perform this Sales finance/configuration action."
        )
    payment = get_object_or_404(
        SalesPayment.objects.select_related("invoice", "organization"),
        pk=payment_id,
        organization=_organization(request),
    )
    try:
        refund_gateway_payment(
            payment=payment,
            amount=request.POST.get("amount"),
            actor=request.crm_user,
            note=request.POST.get("note", ""),
        )
    except (ValidationError, SalesGatewayError, ValueError) as exc:
        text = "; ".join(getattr(exc, "messages", None) or [str(exc)])
        messages.error(request, text)
    else:
        messages.success(request, "Gateway refund recorded.")
    return redirect("shvya-sales-document-detail", document_id=payment.invoice_id)


@crm_login_required
@require_POST
def sales_credit_note_create_view(request, document_id):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can perform this Sales finance/configuration action."
        )
    invoice = _document(request, document_id)
    try:
        note = create_credit_note(
            invoice=invoice,
            amount=request.POST.get("amount"),
            reason=request.POST.get("reason", ""),
            actor=request.crm_user,
            apply=request.POST.get("save_as_draft") != "on",
        )
    except (ValidationError, ValueError) as exc:
        text = "; ".join(getattr(exc, "messages", None) or [str(exc)])
        messages.error(request, text)
    else:
        messages.success(request, f"Credit note {note.credit_number} created.")
    return redirect("shvya-sales-document-detail", document_id=invoice.id)


@crm_login_required
@require_POST
def sales_credit_note_apply_view(request, credit_id):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can perform this Sales finance/configuration action."
        )
    note = get_object_or_404(
        SalesCreditNote.objects.select_related("invoice"),
        pk=credit_id,
        organization=_organization(request),
    )
    try:
        apply_credit_note(credit_note=note, actor=request.crm_user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, f"Credit note {note.credit_number} applied.")
    return redirect("shvya-sales-document-detail", document_id=note.invoice_id)


@crm_login_required
@require_POST
def sales_recurring_invoice_view(request, document_id):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can perform this Sales finance/configuration action."
        )
    invoice = _document(request, document_id)
    if invoice.document_type != DocumentType.INVOICE:
        raise Http404

    action = str(request.POST.get("action") or "save")
    current = SalesRecurringInvoice.objects.filter(source_invoice=invoice).first()
    if action == "disable" and current:
        disable_recurring_rule(current, actor=request.crm_user)
        messages.success(request, "Recurring invoice disabled.")
        return redirect("shvya-sales-document-detail", document_id=invoice.id)

    try:
        interval_count = int(request.POST.get("interval_count") or 1)
        max_cycles_raw = str(request.POST.get("max_cycles") or "").strip()
        max_cycles = int(max_cycles_raw) if max_cycles_raw else None
        next_run = datetime.fromisoformat(request.POST.get("next_run_at"))
        if timezone.is_naive(next_run):
            next_run = timezone.make_aware(
                next_run,
                timezone.get_current_timezone(),
            )
    except (TypeError, ValueError):
        messages.error(request, "Recurring schedule values are invalid.")
        return redirect("shvya-sales-document-detail", document_id=invoice.id)

    if interval_count < 1 or interval_count > 365:
        messages.error(request, "Recurring interval must be between 1 and 365.")
        return redirect("shvya-sales-document-detail", document_id=invoice.id)
    if max_cycles is not None and (max_cycles < 1 or max_cycles > 10000):
        messages.error(request, "Max cycles must be between 1 and 10,000.")
        return redirect("shvya-sales-document-detail", document_id=invoice.id)
    if invoice.status in {SalesDocument.Status.DRAFT, SalesDocument.Status.CANCELLED}:
        messages.error(request, "Send the invoice before making it recurring.")
        return redirect("shvya-sales-document-detail", document_id=invoice.id)
    if next_run <= timezone.now():
        messages.error(request, "Next recurring invoice time must be in the future.")
        return redirect("shvya-sales-document-detail", document_id=invoice.id)

    unit = str(request.POST.get("interval_unit") or "month")
    if unit not in SalesRecurringInvoice.IntervalUnit.values:
        messages.error(request, "Recurring interval unit is invalid.")
        return redirect("shvya-sales-document-detail", document_id=invoice.id)

    rule, _ = SalesRecurringInvoice.objects.update_or_create(
        source_invoice=invoice,
        defaults={
            "organization": invoice.organization,
            "interval_count": interval_count,
            "interval_unit": unit,
            "next_run_at": next_run,
            "max_cycles": max_cycles,
            "auto_send_email": request.POST.get("auto_send_email") == "on",
            "is_active": True,
            "created_by": request.crm_user,
        },
    )
    record_activity(
        invoice,
        event_type="recurring_invoice_configured",
        message="Recurring invoice schedule configured.",
        actor=request.crm_user,
        metadata={"recurring_rule_id": str(rule.id)},
    )
    messages.success(request, "Recurring invoice schedule saved.")
    return redirect("shvya-sales-document-detail", document_id=invoice.id)


@crm_login_required
@require_POST
def sales_schedule_cancel_view(request, schedule_id):
    schedule = get_object_or_404(
        SalesScheduledDelivery.objects.select_related("document"),
        pk=schedule_id,
        organization=_organization(request),
    )
    if schedule.status == SalesScheduledDelivery.Status.PENDING:
        schedule.status = SalesScheduledDelivery.Status.CANCELLED
        schedule.save(update_fields=["status"])
        record_activity(
            schedule.document,
            event_type="scheduled_delivery_cancelled",
            message="Scheduled delivery cancelled.",
            actor=request.crm_user,
            metadata={"schedule_id": str(schedule.id)},
        )
        messages.success(request, "Scheduled send cancelled.")
    return redirect("shvya-sales-document-detail", document_id=schedule.document_id)


@crm_login_required
def sales_settings_view(request):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can perform this Sales finance/configuration action."
        )
    organization = _organization(request)
    sales_settings, _ = SalesSettings.objects.get_or_create(organization=organization)
    gateways = {
        gateway.provider: gateway
        for gateway in SalesPaymentGateway.objects.filter(organization=organization)
    }

    if request.method == "POST":
        action = str(request.POST.get("action") or "")
        if action == "reminders":
            try:
                sales_settings.quotation_expiry_reminder_days = min(
                    90, max(0, int(request.POST.get("quotation_expiry_reminder_days") or 3))
                )
                sales_settings.agreement_expiry_reminder_days = min(
                    365, max(0, int(request.POST.get("agreement_expiry_reminder_days") or 7))
                )
                sales_settings.invoice_due_reminder_days = min(
                    90, max(0, int(request.POST.get("invoice_due_reminder_days") or 3))
                )
                sales_settings.invoice_overdue_repeat_days = min(
                    90, max(1, int(request.POST.get("invoice_overdue_repeat_days") or 3))
                )
            except ValueError:
                messages.error(request, "Reminder values must be whole numbers.")
            else:
                sales_settings.automatic_email_reminders = (
                    request.POST.get("automatic_email_reminders") == "on"
                )
                sales_settings.attach_customer_files_to_email = (
                    request.POST.get("attach_customer_files_to_email") == "on"
                )
                sales_settings.quotation_reminder_subject = (
                    request.POST.get("quotation_reminder_subject")
                    or sales_settings.quotation_reminder_subject
                )[:255]
                sales_settings.quotation_reminder_body = (
                    request.POST.get("quotation_reminder_body")
                    or sales_settings.quotation_reminder_body
                )
                sales_settings.agreement_reminder_subject = (
                    request.POST.get("agreement_reminder_subject")
                    or sales_settings.agreement_reminder_subject
                )[:255]
                sales_settings.agreement_reminder_body = (
                    request.POST.get("agreement_reminder_body")
                    or sales_settings.agreement_reminder_body
                )
                sales_settings.invoice_due_subject = (
                    request.POST.get("invoice_due_subject")
                    or sales_settings.invoice_due_subject
                )[:255]
                sales_settings.invoice_due_body = (
                    request.POST.get("invoice_due_body")
                    or sales_settings.invoice_due_body
                )
                sales_settings.invoice_overdue_subject = (
                    request.POST.get("invoice_overdue_subject")
                    or sales_settings.invoice_overdue_subject
                )[:255]
                sales_settings.invoice_overdue_body = (
                    request.POST.get("invoice_overdue_body")
                    or sales_settings.invoice_overdue_body
                )
                sales_settings.save()
                messages.success(request, "Sales reminder settings saved.")
            return redirect("shvya-sales-settings")

        if action == "gateway":
            provider = str(request.POST.get("provider") or "")
            if provider not in SalesPaymentGateway.Provider.values:
                raise Http404
            gateway, _ = SalesPaymentGateway.objects.get_or_create(
                organization=organization,
                provider=provider,
            )
            gateway.display_name = str(request.POST.get("display_name") or "")[:120]
            gateway.public_key = str(request.POST.get("public_key") or "")[:255]
            secret = request.POST.get("secret")
            webhook_secret = request.POST.get("webhook_secret")
            if secret:
                gateway.set_secret(secret)
            if webhook_secret:
                gateway.set_webhook_secret(webhook_secret)

            requested_enabled = request.POST.get("is_enabled") == "on"
            missing = []
            if provider == SalesPaymentGateway.Provider.RAZORPAY and not gateway.public_key:
                missing.append("Key ID")
            if not gateway.get_secret():
                missing.append("secret key")
            if not gateway.get_webhook_secret():
                missing.append("webhook signing secret")

            gateway.is_enabled = requested_enabled and not missing
            gateway.save()
            if requested_enabled and missing:
                messages.warning(
                    request,
                    (
                        f"{gateway.get_provider_display()} was saved but left disabled. "
                        f"Add: {', '.join(missing)}."
                    ),
                )
            else:
                messages.success(
                    request,
                    f"{gateway.get_provider_display()} settings saved.",
                )
            return redirect("shvya-sales-settings")

    return render(
        request,
        "sales/settings.html",
        {
            "sales_settings": sales_settings,
            "razorpay": gateways.get(SalesPaymentGateway.Provider.RAZORPAY),
            "stripe": gateways.get(SalesPaymentGateway.Provider.STRIPE),
            "email_event_webhook_url": request.build_absolute_uri(
                reverse("shvya-sales-email-provider-event")
            ),
        },
    )


@crm_login_required
@require_POST
def sales_payment_link_create_view(request, document_id):
    if not is_sales_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can perform this Sales finance/configuration action."
        )
    invoice = _document(request, document_id)
    if invoice.document_type != DocumentType.INVOICE:
        raise Http404
    gateway = get_object_or_404(
        SalesPaymentGateway,
        pk=request.POST.get("gateway_id"),
        organization=invoice.organization,
        is_enabled=True,
    )
    try:
        create_payment_checkout(
            invoice=invoice,
            gateway=gateway,
            actor=request.crm_user,
            base_url=request.build_absolute_uri("/"),
        )
    except (ValidationError, SalesGatewayError) as exc:
        text = "; ".join(getattr(exc, "messages", None) or [str(exc)])
        messages.error(request, text)
    else:
        messages.success(request, "Payment link created and ready to share.")
    return redirect("shvya-sales-document-detail", document_id=invoice.id)


@csrf_exempt
@require_POST
def sales_payment_webhook_view(request, gateway_id):
    gateway = get_object_or_404(SalesPaymentGateway, pk=gateway_id, is_enabled=True)
    if gateway.provider == SalesPaymentGateway.Provider.RAZORPAY:
        if not verify_razorpay_webhook(
            gateway=gateway,
            body=request.body,
            signature=request.headers.get("X-Razorpay-Signature"),
        ):
            return HttpResponseForbidden("Invalid signature.")
        try:
            checkout = apply_razorpay_event(gateway=gateway, body=request.body)
        except (ValueError, json.JSONDecodeError, SalesGatewayError) as exc:
            return HttpResponseBadRequest(str(exc) or "Invalid payload.")
    elif gateway.provider == SalesPaymentGateway.Provider.STRIPE:
        if not verify_stripe_webhook(
            gateway=gateway,
            body=request.body,
            signature_header=request.headers.get("Stripe-Signature"),
        ):
            return HttpResponseForbidden("Invalid signature.")
        try:
            checkout = apply_stripe_event(gateway=gateway, body=request.body)
        except (ValueError, json.JSONDecodeError, SalesGatewayError) as exc:
            return HttpResponseBadRequest(str(exc) or "Invalid payload.")
    else:
        raise Http404
    return JsonResponse(
        {
            "status": "accepted",
            "checkout_id": str(checkout.id) if checkout else None,
        }
    )
