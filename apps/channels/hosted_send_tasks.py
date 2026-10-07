"""Delivery for Hosted Account outbound messages.

Manual inbox replies make their first attempt in the request process. Automation
and provider retries use Celery; both share the same durable claim and gateway.
Meta WhatsApp API sends stay in apps.channels.tasks.
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
_CONFIRMED_STATUSES = {
    WhatsAppMessage.Status.SENT,
    WhatsAppMessage.Status.DELIVERED,
    WhatsAppMessage.Status.READ,
}


def _is_manual_hosted_message(message):
    """Only server-labelled agent messages may bypass automation admission."""
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    metadata = payload.get("shvya_hosted")
    return (
        message.direction == WhatsAppMessage.Direction.OUTBOUND
        and isinstance(metadata, dict)
        and metadata.get("origin") == "agent"
        and not any(
            key in payload
            for key in (
                "shvya_ai", "shvya_welcome", "shvya_auto_followup",
                "shvya_workflow", "shvya_sales",
            )
        )
    )


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
        if message.status in _CONFIRMED_STATUSES:
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
def send_hosted_whatsapp_message_task(self, message_id, *, immediate=False):
    # Claim the durable row before any provider call. A direct manual attempt
    # and a Celery delivery therefore cannot race into Chromium for this row.
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
        manual = _is_manual_hosted_message(message)
        if immediate and not manual:
            return {"status": "skipped", "reason": "not_manual"}
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

            # Human replies do not consume or wait for automation admission.
            # Gateway rate limits, connection checks and ACKs still apply.
            # AI admission stays inside its shared 45-second send gate.
            allowed, retry_after, scope = True, None, "account"
            if not manual and not is_ai_message(message):
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

    if immediate:
        from services.channels.hosted_chat_service import (
            chat_key_for_message, queue_hosted_chat_refresh,
        )
        try:
            queue_hosted_chat_refresh(
                account_id=message.account_id,
                reason="sending",
                chat_key=chat_key_for_message(message),
            )
        except Exception:
            # Realtime infrastructure must not prevent a manual provider call.
            logger.exception("Could not publish Hosted sending state for %s", message.id)

    def retry_gateway_error(exc, *, safe_replay=False):
        try:
            code = (json.loads(exc.response_body or "{}") or {}).get("code")
        except (ValueError, TypeError, AttributeError):
            code = None
        # Checking an existing provider request does not consume a new send
        # attempt. Its deadline is enforced by the transport's persisted
        # attempted_at, including across task/worker restarts.
        ack_pending = code == "provider_ack_pending"
        if code in {"provider_outcome_uncertain", "request_payload_conflict"}:
            current = _set_message_state(message.id, status=WhatsAppMessage.Status.FAILED, error=exc)
            if current and current.status in _CONFIRMED_STATUSES:
                _cleanup_upload(current)
                return {"status": "sent", "message_id": str(current.id)}
            _cleanup_upload(message)
            return {"status": "failed", "reason": code, "error": str(exc)}
        if safe_replay or exc.status_code in {404, 409, 425, 429, 503}:
            if not ack_pending and self.request.retries >= self.max_retries:
                current = _set_message_state(message.id, status=WhatsAppMessage.Status.FAILED, error=exc)
                if current and current.status in _CONFIRMED_STATUSES:
                    _cleanup_upload(current)
                    return {"status": "sent", "message_id": str(current.id)}
                _cleanup_upload(message)
                return {"status": "failed", "reason": "provider_retry_limit_reached", "error": str(exc)}
            current = _set_message_state(message.id, status=WhatsAppMessage.Status.QUEUED)
            if current and current.status in _CONFIRMED_STATUSES:
                _cleanup_upload(current)
                return {"status": "sent", "message_id": str(current.id)}
            countdown = _hosted_retry_delay(
                message_id=message.id, retries=self.request.retries,
                retry_after=15 if ack_pending else getattr(exc, "retry_after", None),
            )
            if exc.status_code == 429:
                from apps.core.observability import increment
                increment("messaging.provider_throttled", labels={"provider": "hosted_whatsapp"})
            if immediate or ack_pending:
                # Celery.retry() in a direct call raises into the HTTP request;
                # eager apply() can instead retry immediately in a loop. Publish
                # only an actual provider retry, retaining this row/request ID.
                try:
                    self.apply_async(
                        args=[str(message.id)], countdown=countdown,
                        retries=self.request.retries + (0 if ack_pending else 1), retry=False,
                    )
                except Exception:
                    logger.exception("Could not schedule Hosted manual retry for %s", message.id)
                    error = (
                        "WhatsApp has not confirmed this message and the retry service "
                        "is unavailable. Check delivery before sending it again."
                    )
                    _set_message_state(message.id, status=WhatsAppMessage.Status.FAILED, error=error)
                    _cleanup_upload(message)
                    return {"status": "failed", "reason": "retry_unavailable", "error": error}
                return {
                    "status": "deferred", "reason": code or "gateway_retry",
                    "retry_after": countdown,
                }
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
        if getattr(exc, "reason", "") in {"provider_transient", "provider_ack_pending"} and isinstance(exc.__cause__, WhatsAppWebGatewayError):
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
