"""Celery transport for Hosted Account outbound messages.

Meta WhatsApp API sends stay in apps.channels.tasks. Hosted sends always use the
private whatsapp-web.js gateway so the two connection types cannot cross-route.
"""

import logging

from celery import shared_task
from django.core.files.storage import default_storage

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp_web import (
    WhatsAppWebClient,
    WhatsAppWebGatewayError,
)

logger = logging.getLogger(__name__)


def _cleanup_upload(message):
    payload = message.media_payload if isinstance(message.media_payload, dict) else {}
    path = str(payload.get("storage_path") or "")
    if path:
        try:
            default_storage.delete(path)
        except Exception as exc:
            logger.warning("Could not delete Hosted temporary upload %s: %s", path, exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=20)
def send_hosted_whatsapp_message_task(self, message_id):
    message = (
        WhatsAppMessage.objects.select_related("account", "organization")
        .filter(id=message_id)
        .first()
    )
    if not message:
        return {"status": "skipped", "reason": "message_not_found"}

    account = message.account
    if account.connection_type != "hosted":
        return {"status": "skipped", "reason": "not_hosted"}
    if message.status in {
        WhatsAppMessage.Status.SENT,
        WhatsAppMessage.Status.DELIVERED,
        WhatsAppMessage.Status.READ,
    }:
        return {"status": "skipped", "reason": "already_sent"}
    if message.status != WhatsAppMessage.Status.QUEUED:
        return {"status": "skipped", "reason": "message_not_queued"}
    if not account.is_active or account.status != WhatsAppAccount.Status.CONNECTED:
        message.status = WhatsAppMessage.Status.FAILED
        message.error = "Hosted WhatsApp session is not connected."
        message.save(update_fields=["status", "error", "updated_at"])
        _cleanup_upload(message)
        return {"status": "failed", "reason": "session_not_connected"}

    client = WhatsAppWebClient()
    try:
        if message.message_type == WhatsAppMessage.MessageType.TEXT:
            result = client.send_message(
                session_id=account.id,
                to_number=message.to_number,
                body=message.body,
            )
        else:
            payload = message.media_payload if isinstance(message.media_payload, dict) else {}
            if payload.get("source") != "storage" or not payload.get("storage_path"):
                raise ValueError("Hosted media message has no temporary upload.")
            path = str(payload["storage_path"])
            with default_storage.open(path, "rb") as file_obj:
                result = client.send_uploaded_media(
                    session_id=account.id,
                    to_number=message.to_number,
                    file_obj=file_obj,
                    message_type=message.message_type,
                    mime_type=str(payload.get("mime_type") or "application/octet-stream"),
                    filename=str(payload.get("filename") or "attachment"),
                    caption=message.body,
                )

    except WhatsAppWebGatewayError as exc:
        if exc.status_code is None or exc.status_code >= 500:
            raise self.retry(exc=exc)
        message.status = WhatsAppMessage.Status.FAILED
        message.error = str(exc)
        message.save(update_fields=["status", "error", "updated_at"])
        _cleanup_upload(message)
        return {"status": "failed", "error": str(exc)}
    except (OSError, ValueError) as exc:
        message.status = WhatsAppMessage.Status.FAILED
        message.error = str(exc)
        message.save(update_fields=["status", "error", "updated_at"])
        _cleanup_upload(message)
        return {"status": "failed", "error": str(exc)}

    raw_message_id = str(result.get("messageId") or "").strip()
    existing_payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    message.status = WhatsAppMessage.Status.SENT
    message.error = ""
    if raw_message_id:
        message.external_id = f"wweb:{raw_message_id}"
    message.raw_payload = {
        **existing_payload,
        "gateway_send": result,
    }
    message.save(
        update_fields=["status", "external_id", "raw_payload", "error", "updated_at"]
    )
    _cleanup_upload(message)
    return {"status": "sent", "message_id": str(message.id)}
