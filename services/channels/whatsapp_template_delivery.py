"""Queue and transport approved WhatsApp templates from the Chats inbox."""

from functools import wraps

from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.providers.whatsapp import WhatsAppAPIError

from . import whatsapp_service as base
from .template_service import render_template_body, state_for

_INSTALLED = False


class WhatsAppTemplateSendError(Exception):
    pass


def _lead_values(*, lead, user=None):
    values = {
        "lead_name": lead.name or "",
        "lead_first_name": (lead.name or "").split(" ")[0],
        "phone": lead.phone or "",
        "email": lead.email or "",
        "lead_source": getattr(lead, "lead_source", "") or "",
        "org_name": lead.organization.name or "",
        "user_name": getattr(user, "name", "") or getattr(user, "email", "") or "",
        "pipeline_name": lead.pipeline.name if lead.pipeline_id else "",
        "stage_name": lead.stage.name if lead.stage_id else "",
    }
    values.update(getattr(lead, "attributes", None) or {})
    return values


def _body_components(*, template, lead, user=None):
    state = state_for(template)
    mapping = state.placeholder_mapping if isinstance(state.placeholder_mapping, dict) else {}
    if not mapping:
        return []

    values = _lead_values(lead=lead, user=user)
    ordered_numbers = sorted(mapping, key=lambda value: int(value))
    parameters = [
        {
            "type": "text",
            "text": str(values.get(mapping[number], "") or ""),
        }
        for number in ordered_numbers
    ]
    return [{"type": "body", "parameters": parameters}]


def _template_button_snapshot(button):
    """Return a safe, durable UI snapshot for one Meta template button."""
    if not isinstance(button, dict):
        return None

    kind = str(button.get("type") or "").strip()
    text = str(button.get("text") or "").strip()
    detail = ""
    if kind == "visit_website":
        text = text or "Visit website"
        detail = str(button.get("url") or "").strip()
    elif kind == "call_phone":
        text = text or "Call"
        detail = str(button.get("phone_number") or "").strip()
    elif kind == "copy_offer":
        text = text or "Copy code"
        detail = str(button.get("coupon_code") or "").strip()
    elif kind == "text_back":
        text = text or "Quick reply"
    else:
        text = text or kind.replace("_", " ").title()

    if not text and not detail:
        return None
    return {
        "type": kind,
        "text": text[:80],
        "detail": detail[:500],
    }


def _meta_component(components, kind):
    kind = str(kind or "").upper()
    for item in components or []:
        if isinstance(item, dict) and str(item.get("type") or "").upper() == kind:
            return item
    return {}


def _meta_button_snapshot(button):
    """Normalize a Meta-synced button when the local template JSON is empty."""
    if not isinstance(button, dict):
        return None
    meta_type = str(button.get("type") or "").upper()
    mapping = {
        "URL": "visit_website",
        "PHONE_NUMBER": "call_phone",
        "QUICK_REPLY": "text_back",
        "COPY_CODE": "copy_offer",
    }
    kind = mapping.get(meta_type, meta_type.lower())
    text = str(button.get("text") or "").strip()
    detail = ""
    if meta_type == "URL":
        text = text or "Visit website"
        detail = str(button.get("url") or "").strip()
    elif meta_type == "PHONE_NUMBER":
        text = text or "Call"
        detail = str(button.get("phone_number") or "").strip()
    elif meta_type == "COPY_CODE":
        text = text or "Copy code"
        example = button.get("example")
        if isinstance(example, list):
            detail = str(example[0] if example else "").strip()
        else:
            detail = str(example or button.get("coupon_code") or "").strip()
    elif meta_type == "QUICK_REPLY":
        text = text or "Quick reply"
    else:
        text = text or meta_type.replace("_", " ").title()

    if not text and not detail:
        return None
    return {"type": kind, "text": text[:80], "detail": detail[:500]}


def template_display_snapshot(*, template, rendered_body, state=None):
    """Freeze the complete template presentation used by the Chats inbox.

    The snapshot lives on the WhatsAppMessage so later template edits/deletes do
    not make a previously sent message lose its footer, buttons, or carousel
    presentation. It contains display-only data and no credentials or media
    bytes.
    """
    metadata = state
    if metadata is None:
        try:
            metadata = template.meta_state
        except AttributeError:
            metadata = None
    components = (
        metadata.components
        if metadata is not None and isinstance(metadata.components, list)
        else []
    )

    buttons = []
    for item in template.buttons or []:
        snapshot = _template_button_snapshot(item)
        if snapshot:
            buttons.append(snapshot)
    if not buttons:
        meta_buttons = _meta_component(components, "BUTTONS").get("buttons") or []
        for item in meta_buttons:
            snapshot = _meta_button_snapshot(item)
            if snapshot:
                buttons.append(snapshot)

    footer = str(template.footer or "").strip()
    if not footer:
        footer = str(_meta_component(components, "FOOTER").get("text") or "").strip()

    header = _meta_component(components, "HEADER")
    header_text = str(header.get("text") or "").strip()
    attachment_type = str(template.attachment_type or "none").strip().lower() or "none"
    if attachment_type == "none":
        remote_header_format = str(header.get("format") or "").strip().lower()
        if remote_header_format:
            attachment_type = remote_header_format

    cards = []

    # Avoid creating metadata from a read path. Carousel state is optional and
    # only used when it already exists.
    if template.template_format == WhatsAppTemplate.Format.CAROUSEL and metadata is not None:
        config = metadata.carousel_config if isinstance(metadata.carousel_config, dict) else {}
        for raw_card in (config.get("cards") or [])[:10]:
            if not isinstance(raw_card, dict):
                continue
            card_buttons = []
            for item in (raw_card.get("buttons") or [])[:2]:
                snapshot = _template_button_snapshot(item)
                if snapshot:
                    card_buttons.append(snapshot)
            cards.append(
                {
                    "body": str(raw_card.get("body") or "")[:1024],
                    "media_type": str(raw_card.get("media_type") or "")[:20],
                    "media_name": str(raw_card.get("media_name") or "")[:255],
                    "buttons": card_buttons,
                }
            )

    return {
        "name": str(template.name or "")[:150],
        "category": str(template.category or "")[:20],
        "format": str(template.template_format or "")[:20],
        "body": str(rendered_body or "")[:4096],
        "header_text": header_text[:1024],
        "footer": footer[:60],
        "attachment_type": attachment_type[:20],
        "buttons": buttons,
        "cards": cards,
    }


def queue_template_message(*, template, lead, user=None):
    """Create a queued message that the worker will send as a real Meta template."""
    if template.organization_id != lead.organization_id:
        raise WhatsAppTemplateSendError("Template and lead belong to different organizations.")
    if template.status != WhatsAppTemplate.Status.APPROVED:
        raise WhatsAppTemplateSendError("Only approved WhatsApp templates can be sent.")
    if not template.meta_template_id:
        raise WhatsAppTemplateSendError("This approved template has no Meta template ID. Sync templates first.")

    account = template.account
    if (
        account.status != WhatsAppAccount.Status.CONNECTED
        or not account.is_active
        or not account.phone_number_id
        or not account.access_token
    ):
        raise WhatsAppTemplateSendError("The WhatsApp account for this template is not connected.")

    # Media-header templates need an actual message-time media parameter. The
    # template-creation sample handle cannot be reused as delivered media.
    if template.attachment_type != WhatsAppTemplate.AttachmentType.NONE:
        raise WhatsAppTemplateSendError(
            "This template requires a media header. Sending media-header templates from Chats is not supported yet."
        )

    state = state_for(template)
    body = render_template_body(template=template, lead=lead, user=user)
    components = _body_components(template=template, lead=lead, user=user)

    return base.queue_outbound_message(
        organization=lead.organization,
        account=account,
        to_number=lead.phone,
        body=body,
        lead=lead,
        message_type=WhatsAppMessage.MessageType.TEXT,
        media_payload={
            "transport": "template",
            "template_id": str(template.id),
            "template_name": template.name,
            "language_code": state.language or "en_US",
            "components": components,
            "template_display": template_display_snapshot(
                template=template,
                rendered_body=body,
                state=state,
            ),
        },
    )


def _send_template_transport(message):
    account = message.account
    if account.organization_id != message.organization_id:
        raise base.WhatsAppSendError(
            "WhatsApp account does not belong to the message organization."
        )
    if not account.is_active:
        raise base.WhatsAppSendError("WhatsApp account is inactive.")
    if account.status != WhatsAppAccount.Status.CONNECTED:
        raise base.WhatsAppSendError("WhatsApp account is not connected.")

    payload = message.media_payload if isinstance(message.media_payload, dict) else {}
    template_name = str(payload.get("template_name") or "").strip()
    language_code = str(payload.get("language_code") or "en_US").strip() or "en_US"
    components = payload.get("components") or []
    if not template_name:
        raise base.WhatsAppSendError("Queued WhatsApp template name is missing.")
    if not isinstance(components, list):
        raise base.WhatsAppSendError("Queued WhatsApp template components are invalid.")

    client = base.WhatsAppClient(
        phone_number_id=account.phone_number_id,
        access_token=account.access_token,
    )
    try:
        response = client.send_template_message(
            to=message.to_number,
            template_name=template_name,
            language_code=language_code,
            components=components,
        )
    except WhatsAppAPIError as exc:
        message.status = WhatsAppMessage.Status.FAILED
        message.error = str(exc)
        message.save(update_fields=["status", "error", "updated_at"])
        raise base.WhatsAppSendError(str(exc)) from exc

    messages = response.get("messages") or [] if isinstance(response, dict) else []
    external_id = messages[0].get("id") if messages else None

    existing_payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    ai_metadata = existing_payload.get("shvya_ai")
    final_payload = dict(response) if isinstance(response, dict) else {}
    if ai_metadata is not None:
        final_payload["shvya_ai"] = ai_metadata

    for key in (
        "shvya_welcome",
        "shvya_auto_followup",
        "shvya_workflow",
        "shvya_sales",
    ):
        if key in existing_payload:
            final_payload[key] = existing_payload[key]

    message.status = WhatsAppMessage.Status.SENT
    message.external_id = external_id
    message.raw_payload = final_payload
    message.error = ""
    message.save(
        update_fields=["status", "external_id", "raw_payload", "error", "updated_at"]
    )
    return message


def install_whatsapp_template_transport():
    """Teach the existing Celery send task to recognize queued template transport."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_send = base.send_outbound_message

    @wraps(original_send)
    def send_outbound_message(*, message):
        payload = message.media_payload if isinstance(message.media_payload, dict) else {}
        if payload.get("transport") == "template":
            return _send_template_transport(message)
        return original_send(message=message)

    base.send_outbound_message = send_outbound_message
    _INSTALLED = True
