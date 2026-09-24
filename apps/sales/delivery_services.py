"""Sales document delivery and provider synchronization services."""

from __future__ import annotations

import re
import uuid
from urllib.parse import urljoin

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.urls import reverse
from django.utils import timezone

from apps.sales.models import DocumentType, SalesDocument, SalesDocumentDelivery

from .document_services import SalesDeliveryError, _money_text, merge_values, render_text_template


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

    public_url = urljoin(str(base_url or "").rstrip("/") + "/", reverse(
        "shvya-sales-public-document", args=[document.public_token]).lstrip("/"))
    values = merge_values(document, public_url=public_url)
    body = render_text_template(body, values, strict=True)
    subject = render_text_template(subject, values, strict=True)
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
        for key, value in (getattr(lead, "attributes", None) or {}).items():
            values.setdefault(key, value)
    values.update(merge_values(document, public_url=public_url))
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
            if key not in values or values[key] in (None, ""):
                raise SalesDeliveryError(f"Provide a value for WhatsApp template variable: {key}")
            value = str(values[key])
            parameters.append({"type": "text", "text": value})
            rendered_body = re.sub(
                r"{{\s*(?:" + re.escape(str(key)) + "|" + re.escape(str(number)) + r")\s*}}",
                lambda match: value, rendered_body,
            )
        components.append({"type": "body", "parameters": parameters})

    if re.search(r"{{\s*[^{}]+\s*}}", rendered_body):
        raise SalesDeliveryError("Map every WhatsApp template variable before sending.")
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

    from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
    from apps.channels.tasks import send_whatsapp_message_task
    from services.crm.lead_chat import pipeline_chat_account

    public_url = urljoin(str(base_url or "").rstrip("/") + "/", reverse(
        "shvya-sales-public-document", args=[document.public_token]).lstrip("/"))
    values = merge_values(document, public_url=public_url)
    body = render_text_template(body, values, strict=True)
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
        hosted_pdf_bytes = None
        if attach_pdf:
            if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
                # Hosted linked-device sends must stream the immutable PDF through
                # SHVYA's private uploaded-media endpoint. Passing a public URL to
                # whatsapp-web.js makes the gateway reconstruct remote media and
                # can fail inside WhatsApp Web's memoized media getters.
                from apps.sales.pdf_service import read_document_pdf

                hosted_pdf_bytes = read_document_pdf(document, actor=user)
            else:
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

        dispatch_task = send_whatsapp_message_task

        if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
            from apps.channels.hosted_send_tasks import (
                send_hosted_whatsapp_message_task,
            )

            dispatch_task = send_hosted_whatsapp_message_task
            if attach_pdf:
                from django.core.files.uploadedfile import SimpleUploadedFile

                from services.channels.hosted_send_service import (
                    queue_hosted_uploaded_media,
                )

                if not hosted_pdf_bytes:
                    raise SalesDeliveryError(
                        "The document PDF could not be prepared for Hosted WhatsApp."
                    )
                upload = SimpleUploadedFile(
                    f"{document.document_number}.pdf",
                    hosted_pdf_bytes,
                    content_type="application/pdf",
                )
                message = queue_hosted_uploaded_media(
                    account=account,
                    to_number=document.lead.phone,
                    uploaded_file=upload,
                    message_type=WhatsAppMessage.MessageType.DOCUMENT,
                    caption=delivery.body,
                    lead=document.lead,
                )
            else:
                from services.channels.hosted_whatsapp_service import (
                    queue_hosted_text_message,
                )

                message = queue_hosted_text_message(
                    account=account,
                    to_number=document.lead.phone,
                    body=delivery.body,
                    lead=document.lead,
                    metadata={
                        "origin": "agent",
                        "chat_id": document.lead.phone,
                    },
                )
        elif requires_template or whatsapp_template_id:
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
        dispatch_task.delay(str(message.id))
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
            "template_used": bool(requires_template or (whatsapp_template_id and account.connection_type == WhatsAppAccount.ConnectionType.API)),
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
