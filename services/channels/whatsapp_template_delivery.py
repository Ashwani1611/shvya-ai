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
