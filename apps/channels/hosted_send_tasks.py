"""Celery transport for Hosted Account outbound messages.

Meta WhatsApp API sends stay in apps.channels.tasks. Hosted sends always use the
private whatsapp-web.js gateway so the two connection types cannot cross-route.
"""

import logging
import json

from celery import shared_task
from django.core.files.storage import default_storage
from django.db import transaction

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp_web import WhatsAppWebGatewayError

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
        payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        if any(
            isinstance(payload.get(key), dict) and payload[key].get("job_id")
            for key in ("shvya_ai", "shvya_welcome")
        ):
            return {"status": "skipped", "reason": "durable_ai_job_owned"}
        if not account.is_active or account.status != WhatsAppAccount.Status.CONNECTED:
            message.status = WhatsAppMessage.Status.FAILED
            message.error = "Hosted WhatsApp session is not connected."
            message.save(update_fields=["status", "error", "updated_at"])
            failed_disconnected = True
        else:
            from django.conf import settings
            from apps.core.fairness import admit_provider_start
            from services.channels.ai_send_gate import is_ai_message

            # AI admission belongs inside its shared 45-second send gate.
            # Waiting legacy tasks must not consume every provider token.
            allowed, retry_after, scope = True, None, "account"
            if not is_ai_message(message):
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

    # All Hosted delivery uses the same final transport boundary. In particular,
    # old ETA welcomes must not bypass live AI permissions or sender pacing.
    from services.channels.ai_send_gate import next_ai_send_at
    from services.channels.hosted_automation_service import HostedAutomationPaused
    from services.channels.hosted_whatsapp_transport import send_hosted_message
    from services.channels.whatsapp_service import WhatsAppSendError

    def retry_gateway_error(exc, *, safe_replay=False):
        try:
            code = (json.loads(exc.response_body or "{}") or {}).get("code")
        except (ValueError, TypeError, AttributeError):
            code = None
        if code in {"provider_outcome_uncertain", "request_payload_conflict"}:
            _set_message_state(message.id, status=WhatsAppMessage.Status.FAILED, error=exc)
            _cleanup_upload(message)
            return {"status": "failed", "reason": code, "error": str(exc)}
        if safe_replay or exc.status_code in {404, 409, 425, 429, 503}:
            if self.request.retries >= self.max_retries:
                _set_message_state(message.id, status=WhatsAppMessage.Status.FAILED, error=exc)
                _cleanup_upload(message)
                return {"status": "failed", "reason": "provider_retry_limit_reached", "error": str(exc)}
            _set_message_state(message.id, status=WhatsAppMessage.Status.QUEUED)
            countdown = _hosted_retry_delay(
                message_id=message.id, retries=self.request.retries,
                retry_after=getattr(exc, "retry_after", None),
            )
            if exc.status_code == 429:
                from apps.core.observability import increment
                increment("messaging.provider_throttled", labels={"provider": "hosted_whatsapp"})
            raise self.retry(exc=exc, countdown=countdown)
        uncertain = exc.status_code is None or exc.status_code >= 500
        _set_message_state(
            message.id, status=WhatsAppMessage.Status.FAILED,
            error=f"Provider outcome is uncertain: {exc}" if uncertain else exc,
        )
        _cleanup_upload(message)
        return {
            "status": "failed",
            "reason": "provider_outcome_uncertain" if uncertain else "gateway_rejected",
            "error": str(exc),
        }

    try:
        send_hosted_message(message=message)
    except HostedAutomationPaused as exc:
        # Provider retries keep their existing bounded retry budget. The
        # canonical transport persists a stable request identity so an AI
        # retry reconciles an uncertain send rather than issuing a duplicate.
        if getattr(exc, "reason", "") == "provider_transient" and isinstance(exc.__cause__, WhatsAppWebGatewayError):
            return retry_gateway_error(exc.__cause__, safe_replay=True)
        _set_message_state(message.id, status=WhatsAppMessage.Status.QUEUED)
        available_at = exc.paused_until
        gate_at = next_ai_send_at(account)
        if gate_at and gate_at > available_at:
            available_at = gate_at
        self.apply_async(args=[str(message.id)], eta=available_at)
        return {
            "status": "deferred",
            "reason": getattr(exc, "reason", "account_health_pause"),
            "available_at": available_at.isoformat(),
        }
    except WhatsAppSendError as exc:
        if isinstance(exc.__cause__, WhatsAppWebGatewayError):
            return retry_gateway_error(exc.__cause__)
        _set_message_state(message.id, status=WhatsAppMessage.Status.FAILED, error=exc)
        _cleanup_upload(message)
        return {"status": "failed", "error": str(exc)}
    except (OSError, ValueError) as exc:
        _set_message_state(message.id, status=WhatsAppMessage.Status.FAILED, error=exc)
        _cleanup_upload(message)
        return {"status": "failed", "error": str(exc)}
    except Exception as exc:
        _set_message_state(message.id, status=WhatsAppMessage.Status.FAILED, error=exc)
        _cleanup_upload(message)
        raise

    _cleanup_upload(message)
    return {"status": "sent", "message_id": str(message.id)}
