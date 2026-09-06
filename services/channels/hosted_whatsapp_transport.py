"""Provider dispatch for Hosted Account sends without disturbing Meta Cloud API."""

from apps.channels.models import WhatsAppMessage
from apps.channels.providers.whatsapp_web import (
    WhatsAppWebClient,
    WhatsAppWebGatewayError,
)


_INSTALLED = False
_ORIGINAL_SEND = None


def send_hosted_message(*, message, defer_on_pause=True):
    from services.channels.hosted_automation_service import (
        HostedAutomationPaused,
        automation_pause_until,
        message_is_automation,
        record_hosted_send,
    )
    from services.channels.whatsapp_service import WhatsAppSendError

    account = message.account
    if account.organization_id != message.organization_id:
        raise WhatsAppSendError(
            "WhatsApp account does not belong to the message organization."
        )
    if not account.is_active:
        raise WhatsAppSendError("WhatsApp account is inactive.")
    if account.status != account.Status.CONNECTED:
        raise WhatsAppSendError("Hosted WhatsApp session is not running.")

    if message_is_automation(message):
        paused_until = automation_pause_until(account=account)
        if paused_until:
            if defer_on_pause:
                raise HostedAutomationPaused(paused_until)
            provider_error = WhatsAppWebGatewayError(
                f"Hosted automation paused by Account Health until {paused_until.isoformat()}.",
                status_code=503,
            )
            raise WhatsAppSendError(str(provider_error)) from provider_error

    media_url = None
    filename = None
    if message.message_type != WhatsAppMessage.MessageType.TEXT:
        payload = message.media_payload or {}
        if payload.get("source") != "url" or not payload.get("url"):
            raise WhatsAppSendError(
                "Hosted WhatsApp media requires a URL-backed media source."
            )
        media_url = payload["url"]
        filename = payload.get("filename")

    try:
        response = WhatsAppWebClient().send_message(
            session_id=account.id,
            to_number=message.to_number,
            body=message.body,
            message_type=message.message_type,
            media_url=media_url,
            filename=filename,
        )
    except WhatsAppWebGatewayError as exc:
        message.status = WhatsAppMessage.Status.FAILED
        message.error = str(exc)
        message.save(update_fields=["status", "error", "updated_at"])
        raise WhatsAppSendError(str(exc)) from exc

    raw_id = response.get("messageId")
    if not raw_id:
        message.status = WhatsAppMessage.Status.FAILED
        message.error = "Hosted WhatsApp gateway returned no message id."
        message.save(update_fields=["status", "error", "updated_at"])
        raise WhatsAppSendError(message.error)

    existing_payload = (
        message.raw_payload if isinstance(message.raw_payload, dict) else {}
    )
    final_payload = dict(response)
    for key in ("shvya_ai", "shvya_hosted", "shvya_auto_followup"):
        if key in existing_payload:
            final_payload[key] = existing_payload[key]

    message.status = WhatsAppMessage.Status.SENT
    message.external_id = f"wweb:{raw_id}"
    message.raw_payload = final_payload
    message.error = ""
    message.save(
        update_fields=[
            "status",
            "external_id",
            "raw_payload",
            "error",
            "updated_at",
        ]
    )
    record_hosted_send(account=account, message=message)

    hosted_meta = existing_payload.get("shvya_hosted") or {}
    if hosted_meta.get("origin") == "agent" and message.lead_id:
        from services.channels.hosted_automation_service import register_hosted_manual_outbound

        register_hosted_manual_outbound(
            account=account,
            lead=message.lead,
            at=timezone.now() if False else message.updated_at,
        )
    return message


def install_hosted_whatsapp_transport():
    """Patch the canonical Celery send path with provider dispatch once."""
    global _INSTALLED, _ORIGINAL_SEND
    if _INSTALLED:
        return

    from services.channels import whatsapp_service

    _ORIGINAL_SEND = whatsapp_service.send_outbound_message

    def provider_aware_send_outbound_message(*, message):
        if message.account.connection_type == "hosted":
            return send_hosted_message(message=message, defer_on_pause=False)
        return _ORIGINAL_SEND(message=message)

    whatsapp_service.send_outbound_message = provider_aware_send_outbound_message
    _INSTALLED = True
