"""Celery transport for Hosted Account outbound messages.

Meta WhatsApp API sends stay in apps.channels.tasks. Hosted sends always use the
private whatsapp-web.js gateway so the two connection types cannot cross-route.
"""

import logging

from celery import shared_task
from django.core.files.storage import default_storage
from django.db import transaction

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp_web import (
    WhatsAppWebClient,
    WhatsAppWebGatewayError,
)
from services.channels.hosted_health_guard import (
    finalize_hosted_send,
    message_is_hosted_automation,
    release_hosted_automation_reservation,
    reserve_hosted_automation_send,
)

logger = logging.getLogger(__name__)

_HOSTED_SENDING_STATUS = "sending"


def _hosted_retry_delay(*, message_id, retries, retry_after=None):
    if retry_after is not None:
        try:
            base = max(1, min(int(retry_after), 900))
        except (TypeError, ValueError):
            base = 20
    else:
        base = min(180, 10 * (2 ** max(0, int(retries or 0))))
    jitter = (
        sum(ord(character) for character in str(message_id))
        + (int(retries or 0) * 13)
    ) % 7
    return base + jitter


def _set_message_state(message_id, *, status, error=""):
    with transaction.atomic():
        message = (
            WhatsAppMessage.objects.select_for_update()
            .filter(pk=message_id)
            .first()
        )
        if message is None:
            return None
        if message.status in {
            WhatsAppMessage.Status.SENT,
            WhatsAppMessage.Status.DELIVERED,
            WhatsAppMessage.Status.READ,
        }:
            return message
        message.status = status
        message.error = str(error or "")[:1000]
        message.save(update_fields=["status", "error", "updated_at"])
        return message


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
    # Claim the durable row before any provider call. Multiple Celery
    # deliveries for the same message can therefore never race into Chromium.
    with transaction.atomic():
        message = (
            WhatsAppMessage.objects.select_for_update()
            .select_related("account", "organization")
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
            failed_disconnected = True
        else:
            from django.conf import settings
            from apps.core.fairness import admit_provider_start

            allowed, retry_after, scope = admit_provider_start(
                provider="hosted_whatsapp",
                account_id=account.id,
                account_limit=settings.HOSTED_WHATSAPP_ACCOUNT_SENDS_PER_MINUTE,
                global_limit=settings.HOSTED_WHATSAPP_GLOBAL_SENDS_PER_MINUTE,
            )
            if not allowed:
                countdown = _hosted_retry_delay(
                    message_id=message.id,
                    retries=self.request.retries,
                    retry_after=retry_after,
                )
                self.apply_async(
                    args=[str(message.id)],
                    countdown=countdown,
                )
                return {
                    "status": "deferred",
                    "reason": f"hosted_whatsapp_{scope}_fairness_limit",
                    "retry_after": countdown,
                }

            message.status = _HOSTED_SENDING_STATUS
            message.error = ""
            message.save(update_fields=["status", "error", "updated_at"])
            failed_disconnected = False

    if failed_disconnected:
        _cleanup_upload(message)
        return {"status": "failed", "reason": "session_not_connected"}

    # This worker is also used by direct Hosted UI sends and by the AI-generated
    # new-lead welcome path. Only SHVYA automation is subject to Account Health;
    # human agent messages remain sendable while automation is paused.
    is_automation = message_is_hosted_automation(message)
    reservation_acquired = False
    if is_automation:
        try:
            gate = reserve_hosted_automation_send(account=account)
        except Exception as exc:
            _set_message_state(
                message.id,
                status=WhatsAppMessage.Status.FAILED,
                error=exc,
            )
            _cleanup_upload(message)
            raise
        blocked_until = gate.get("blocked_until")
        if blocked_until:
            _set_message_state(
                message.id,
                status=WhatsAppMessage.Status.QUEUED,
            )
            self.apply_async(args=[str(message.id)], eta=blocked_until)
            return {
                "status": "deferred",
                "reason": "account_health_pause",
                "available_at": blocked_until.isoformat(),
            }
        reservation_acquired = bool(gate.get("reserved"))

    provider_confirmed = False
    try:
        try:
            from apps.channels.hosted_gateway_routing import gateway_client_for_account

            client = gateway_client_for_account(
                account,
                client_class=WhatsAppWebClient,
            )
            if message.message_type == WhatsAppMessage.MessageType.TEXT:
                result = client.send_message(
                    session_id=account.id,
                    to_number=message.to_number,
                    body=message.body,
                )
            else:
                payload = (
                    message.media_payload
                    if isinstance(message.media_payload, dict)
                    else {}
                )
                if (
                    payload.get("source") != "storage"
                    or not payload.get("storage_path")
                ):
                    raise ValueError(
                        "Hosted media message has no temporary upload."
                    )
                path = str(payload["storage_path"])
                with default_storage.open(path, "rb") as file_obj:
                    result = client.send_uploaded_media(
                        session_id=account.id,
                        to_number=message.to_number,
                        file_obj=file_obj,
                        message_type=message.message_type,
                        mime_type=str(
                            payload.get("mime_type")
                            or "application/octet-stream"
                        ),
                        filename=str(
                            payload.get("filename")
                            or "attachment"
                        ),
                        caption=message.body,
                    )

            # A normal gateway return means the provider accepted the send.
            # Keep the reservation if a later local DB operation fails because
            # the realtime gateway callback/reconciliation will observe it.
            provider_confirmed = True

        except WhatsAppWebGatewayError as exc:
            # These statuses are produced before the gateway can safely begin a
            # WhatsApp send, so replay is safe. A network timeout or gateway
            # 5xx/502 around sendMessage() is uncertain and must not be replayed.
            if exc.status_code in {404, 409, 425, 429, 503}:
                if self.request.retries >= self.max_retries:
                    _set_message_state(
                        message.id,
                        status=WhatsAppMessage.Status.FAILED,
                        error=exc,
                    )
                    _cleanup_upload(message)
                    return {
                        "status": "failed",
                        "reason": "provider_retry_limit_reached",
                        "error": str(exc),
                    }
                _set_message_state(
                    message.id,
                    status=WhatsAppMessage.Status.QUEUED,
                )
                countdown = _hosted_retry_delay(
                    message_id=message.id,
                    retries=self.request.retries,
                )
                if exc.status_code == 429:
                    from apps.core.observability import increment

                    increment(
                        "messaging.provider_throttled",
                        labels={"provider": "hosted_whatsapp"},
                    )
                raise self.retry(exc=exc, countdown=countdown)

            uncertain = exc.status_code is None or exc.status_code >= 500
            _set_message_state(
                message.id,
                status=WhatsAppMessage.Status.FAILED,
                error=(
                    f"Provider outcome is uncertain: {exc}"
                    if uncertain
                    else exc
                ),
            )
            _cleanup_upload(message)
            return {
                "status": "failed",
                "reason": (
                    "provider_outcome_uncertain"
                    if uncertain
                    else "gateway_rejected"
                ),
                "error": str(exc),
            }
        except (OSError, ValueError) as exc:
            _set_message_state(
                message.id,
                status=WhatsAppMessage.Status.FAILED,
                error=exc,
            )
            _cleanup_upload(message)
            return {"status": "failed", "error": str(exc)}
        except Exception as exc:
            _set_message_state(
                message.id,
                status=WhatsAppMessage.Status.FAILED,
                error=exc,
            )
            _cleanup_upload(message)
            raise
    finally:
        if reservation_acquired and not provider_confirmed:
            release_hosted_automation_reservation(account=account)

    raw_message_id = str(result.get("messageId") or "").strip()
    existing_payload = (
        message.raw_payload
        if isinstance(message.raw_payload, dict)
        else {}
    )
    message.status = WhatsAppMessage.Status.SENT
    message.error = ""
    if raw_message_id:
        message.external_id = f"wweb:{raw_message_id}"
    message.raw_payload = {
        **existing_payload,
        "gateway_send": result,
    }
    message.save(
        update_fields=[
            "status",
            "external_id",
            "raw_payload",
            "error",
            "updated_at",
        ]
    )
    _cleanup_upload(message)

    # Reconcile immediately after every successful Hosted send. This keeps the
    # Account Health counts current for both automation and manual linked-number
    # activity instead of waiting for a later UI refresh or gateway callback.
    finalize_hosted_send(account=account, message=message)

    from services.channels.hosted_chat_service import queue_hosted_chat_refresh

    queue_hosted_chat_refresh(
        account_id=account.id,
        reason=(
            "sent_media"
            if message.message_type != WhatsAppMessage.MessageType.TEXT
            else "sent"
        ),
        chat_key=message.to_number,
    )
    return {"status": "sent", "message_id": str(message.id)}
