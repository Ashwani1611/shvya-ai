"""Provider dispatch for Hosted Account sends without disturbing Meta Cloud API."""

import json
import logging
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.db import transaction
from django.utils import timezone

from services.channels.ai_send_gate import paced_ai_send

from apps.channels.models import WhatsAppMessage
from apps.channels.providers.whatsapp_web import (
    WhatsAppWebClient,
    WhatsAppWebGatewayError,
)


_INSTALLED = False
_ORIGINAL_SEND = None
TRANSIENT_AUTOMATION_RETRY_SECONDS = 15
ACK_WAIT_LIMIT_SECONDS = 300
logger = logging.getLogger(__name__)


def _provider_sent_at(response):
    """A replay confirms the original provider send time, not the retry time."""
    now = timezone.now()
    raw_timestamp = response.get("timestamp") if isinstance(response, dict) else None
    try:
        if isinstance(raw_timestamp, bool) or float(raw_timestamp) <= 0:
            return now
        sent_at = datetime.fromtimestamp(float(raw_timestamp), tz=datetime_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return now
    return min(sent_at, now)


def _push_chat_refresh(message, reason):
    from services.channels.hosted_chat_service import (
        chat_key_for_message,
        queue_hosted_chat_refresh,
    )

    try:
        queue_hosted_chat_refresh(
            account_id=message.account_id,
            reason=reason,
            chat_key=chat_key_for_message(message),
        )
    except Exception:
        # Delivery is authoritative in the database. A websocket/broker outage
        # must not turn an acknowledged send into a failed automation job.
        logger.exception("Could not publish Hosted delivery state for %s", message.pk)


def _mark_send_attempt(message):
    """Persist retry identity before gateway I/O, including across worker death."""
    payload = dict(message.raw_payload or {})
    previous = payload.get("shvya_hosted_request") or {}
    retry = bool(previous.get("attempted_at"))
    payload["shvya_hosted_request"] = {
        "request_id": str(message.pk),
        "attempted_at": previous.get("attempted_at") or timezone.now().isoformat(),
    }
    message.raw_payload = payload
    message.save(update_fields=["raw_payload", "updated_at"])
    return retry


def _record_send_error(message, *, error, failed):
    """Persist a transport error only while delivery remains unconfirmed."""
    with transaction.atomic():
        message.refresh_from_db(from_queryset=WhatsAppMessage.objects.select_for_update().filter(
            organization_id=message.organization_id, account_id=message.account_id,
        ))
        if message.status in {"sent", "delivered", "read"}:
            return True
        message.error = error
        fields = ["error", "updated_at"]
        if failed:
            message.status = WhatsAppMessage.Status.FAILED
            fields.append("status")
        message.save(update_fields=fields)
    return False


@paced_ai_send
def send_hosted_message(*, message, defer_on_pause=True):
    from services.channels.hosted_automation_service import (
        HostedAutomationPaused,
        hosted_ai_block_reason,
    )
    from services.channels.hosted_health_guard import (
        finalize_hosted_send,
        message_is_hosted_automation,
        release_hosted_automation_reservation,
        reserve_hosted_automation_send,
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
        if defer_on_pause and message_is_hosted_automation(message):
            paused = HostedAutomationPaused(timezone.now() + timedelta(seconds=30))
            paused.reason = "session_reconnecting"
            raise paused
        raise WhatsAppSendError("Hosted WhatsApp session is not running.")

    raw_payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    if raw_payload.get("shvya_ai") or raw_payload.get("shvya_welcome"):
        reason = (
            hosted_ai_block_reason(account=account, lead=message.lead)
            if message.lead_id and not raw_payload.get("shvya_welcome")
            else ("" if message.lead_id else "lead_missing")
        )
        if not reason:
            from services.channels.hosted_whatsapp_service import account_ai_block_reason

            ai_metadata = raw_payload.get("shvya_ai") or {}
            reason = account_ai_block_reason(
                account=account, lead=message.lead,
                bump_up_number=(ai_metadata.get("number", 1) if ai_metadata.get("origin") == "bump_up" else None),
            )
        if reason:
            message.status = WhatsAppMessage.Status.FAILED
            message.error = f"AI send cancelled: {reason}"
            message.save(update_fields=["status", "error", "updated_at"])
            raise WhatsAppSendError(message.error)

    if raw_payload.get("shvya_auto_followup"):
        from services.followup_service import live_followup_due

        execution = message.followup_executions.select_related(
            "state__organization", "state__lead__pipeline",
            "state__sequence__whatsapp_account",
        ).filter(organization_id=message.organization_id).first()
        if execution is None:
            raise WhatsAppSendError("Follow-up execution is missing.")
        eligible_at = live_followup_due(execution.state)
        if eligible_at > timezone.now():
            raise HostedAutomationPaused(eligible_at)

    media_url = None
    filename = None
    document = None
    storage_path = None
    if message.message_type != WhatsAppMessage.MessageType.TEXT:
        media_payload = message.media_payload or {}
        if media_payload.get("source") == "document":
            from apps.ai_engagement.services.file_sharing import FileSharingService
            document_id = media_payload.get("document_id")
            if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id <= 0:
                raise WhatsAppSendError("Invalid guided document ID.")
            document = FileSharingService.eligible_documents(
                organization=message.organization,
            ).filter(pk=document_id).first()
            if document is None:
                raise WhatsAppSendError("The selected organization document is no longer available.")
            if raw_payload.get("shvya_ai") and not str(document.share_instruction or "").strip():
                raise WhatsAppSendError("The selected file is no longer configured for AI-guided sharing.")
        elif media_payload.get("source") == "url" and media_payload.get("url"):
            media_url = media_payload["url"]
            filename = media_payload.get("filename")
        elif media_payload.get("source") == "storage" and media_payload.get("storage_path"):
            storage_path = str(media_payload["storage_path"])
        else:
            raise WhatsAppSendError("Hosted WhatsApp media requires a document, uploaded file, or URL-backed source.")

    is_automation = message_is_hosted_automation(message)
    reservation_acquired = False
    if is_automation:
        gate = reserve_hosted_automation_send(account=account)
        blocked_until = gate.get("blocked_until")
        if blocked_until:
            if defer_on_pause:
                raise HostedAutomationPaused(blocked_until)
            provider_error = WhatsAppWebGatewayError(
                f"Hosted automation paused by Account Health until {blocked_until.isoformat()}.",
                status_code=503,
            )
            raise WhatsAppSendError(str(provider_error)) from provider_error
        reservation_acquired = bool(gate.get("reserved"))

    provider_confirmed = False
    request_is_retry = False
    try:
        from apps.channels.hosted_gateway_routing import gateway_client_for_account

        client = gateway_client_for_account(account, client_class=WhatsAppWebClient)
        if storage_path is not None:
            from django.core.files.storage import default_storage

            with default_storage.open(storage_path, "rb") as file_obj:
                request_is_retry = _mark_send_attempt(message)
                response = client.send_uploaded_media(
                    session_id=account.id, to_number=message.to_number, file_obj=file_obj,
                    message_type=message.message_type,
                    mime_type=str(media_payload.get("mime_type") or "application/octet-stream"),
                    filename=str(media_payload.get("filename") or "attachment"),
                    caption=message.body, request_id=str(message.pk),
                    request_is_retry=request_is_retry,
                )
        elif document is not None:
            import mimetypes
            from pathlib import Path
            filename = Path(document.file.name).name
            with document.file.open("rb") as file_obj:
                request_is_retry = _mark_send_attempt(message)
                response = client.send_uploaded_media(
                    session_id=account.id, to_number=message.to_number, file_obj=file_obj,
                    message_type=WhatsAppMessage.MessageType.DOCUMENT,
                    mime_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    filename=filename, caption=message.body, request_id=str(message.pk),
                    request_is_retry=request_is_retry,
                )
        else:
            request_is_retry = _mark_send_attempt(message)
            response = client.send_message(
                session_id=account.id, to_number=message.to_number, body=message.body,
                message_type=message.message_type, media_url=media_url, filename=filename,
                request_id=str(message.pk), request_is_retry=request_is_retry,
            )

        raw_id = response.get("messageId")
        if not raw_id:
            if _record_send_error(
                message, error="Hosted WhatsApp gateway returned no message id.", failed=True,
            ):
                provider_confirmed = True
                finalize_hosted_send(account=account, message=message)
                _push_chat_refresh(message, "sent")
                return message
            _push_chat_refresh(message, "failed")
            raise WhatsAppSendError(message.error)

        # The gateway returns success only after a WhatsApp server ACK, not
        # merely a locally-created ID. Retain the reservation after that ACK
        # even if a later local write fails.
        provider_confirmed = True
    except WhatsAppWebGatewayError as exc:
        try:
            error_payload = json.loads(exc.response_body or "{}") or {}
            error_code = error_payload.get("code")
        except (ValueError, TypeError, AttributeError):
            error_payload = {}
            error_code = None
        if not request_is_retry and error_code == "send_claim_unavailable":
            # Gateway explicitly confirms it did not call WhatsApp. A later
            # first attempt is safe when its claim storage recovers.
            payload = dict(message.raw_payload or {})
            payload.pop("shvya_hosted_request", None)
            message.raw_payload = payload
            message.save(update_fields=["raw_payload", "updated_at"])
        transient = exc.status_code is None or exc.status_code >= 500
        ack_pending = error_code == "provider_ack_pending"
        if ack_pending:
            # The provider ID is correlation evidence, not proof of delivery.
            # Persist it while still queued so the authenticated ACK callback
            # can find this exact row even when browser ACK lookup is stale.
            # Otherwise callbacks retry forever against an empty external_id.
            pending_id = str(error_payload.get("messageId") or "").strip()
            if pending_id:
                external_id = f"wweb:{pending_id}"
                if message.external_id and message.external_id != external_id:
                    raise WhatsAppSendError("Hosted provider message identity changed during acknowledgement.") from exc
                message.external_id = external_id
                message.save(update_fields=["external_id", "updated_at"])
                message.refresh_from_db()
                if message.status in {"sent", "delivered", "read"}:
                    # A callback may win this race. Do not replace confirmed
                    # delivery with a stale HTTP acknowledgement timeout.
                    provider_confirmed = True
                    finalize_hosted_send(account=account, message=message)
                    _push_chat_refresh(message, "sent")
                    return message
            # Query the existing request, never regenerate/resend the content.
            # Bound this wait so an unacknowledged message cannot block every
            # other lead on the same account indefinitely.
            attempted_at = (message.raw_payload or {}).get("shvya_hosted_request", {}).get("attempted_at")
            try:
                started = datetime.fromisoformat(str(attempted_at))
                elapsed = (timezone.now() - started).total_seconds()
            except (ValueError, TypeError):
                elapsed = ACK_WAIT_LIMIT_SECONDS
            if elapsed >= ACK_WAIT_LIMIT_SECONDS:
                transient = False
                # All callers, including legacy Celery transports, must treat
                # this as a terminal uncertain outcome, not another HTTP 503.
                exc = WhatsAppWebGatewayError(
                    "WhatsApp acknowledgement wait expired; outcome is uncertain.",
                    status_code=409,
                    response_body=json.dumps({"code": "provider_outcome_uncertain"}),
                )

        # AI, welcome, and follow-up automation must survive temporary Hosted
        # gateway/network failures. Keep the exact generated message queued and
        # let the durable automation job retry it shortly instead of marking the
        # conversation permanently failed after one transient provider error.
        retrying = defer_on_pause and (is_automation or ack_pending) and transient
        error = (
            f"Temporary Hosted gateway failure; retry scheduled: {exc}"
            if retrying else (
                "WhatsApp did not acknowledge this message within five minutes. "
                "Delivery is unconfirmed; automatic resend is blocked to prevent duplicates."
                if ack_pending and not transient else str(exc)
            )
        )
        # An ACK may commit after the earlier pending-ID refresh. Recheck and
        # persist under one short row lock so neither a timeout nor a retry
        # error can overwrite confirmed delivery. Provider I/O is finished.
        if _record_send_error(message, error=error, failed=not retrying):
            provider_confirmed = True
            finalize_hosted_send(account=account, message=message)
            _push_chat_refresh(message, "sent")
            return message
        if retrying:
            _push_chat_refresh(message, "retrying")
            paused = HostedAutomationPaused(
                timezone.now() + timedelta(seconds=TRANSIENT_AUTOMATION_RETRY_SECONDS)
            )
            paused.reason = "provider_ack_pending" if ack_pending else "provider_transient"
            raise paused from exc

        _push_chat_refresh(message, "failed")
        raise WhatsAppSendError(message.error) from exc
    finally:
        if reservation_acquired and not provider_confirmed:
            release_hosted_automation_reservation(account=account)

    with transaction.atomic():
        # A delivered/read callback can commit while the HTTP send is still
        # awaiting its response. Merge against the locked current row, never
        # downgrade that receipt or erase the callback's identity/metadata.
        message.refresh_from_db(from_queryset=WhatsAppMessage.objects.select_for_update().filter(
            organization_id=message.organization_id, account_id=message.account_id,
        ))
        external_id = f"wweb:{raw_id}"
        if message.external_id and message.external_id != external_id:
            raise WhatsAppSendError("Hosted provider message identity changed during acknowledgement.")
        existing_payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        provider_status = {
            "read": WhatsAppMessage.Status.READ,
            "delivered": WhatsAppMessage.Status.DELIVERED,
        }.get(response.get("status"), WhatsAppMessage.Status.SENT)
        ranks = {"sent": 1, "delivered": 2, "read": 3}
        if ranks.get(provider_status, 0) >= ranks.get(message.status, 0):
            message.status = provider_status
        message.sent_at = _provider_sent_at(response)
        message.external_id = external_id
        message.raw_payload = {**existing_payload, **response}
        message.error = ""
        message.save(update_fields=[
            "status", "external_id", "sent_at", "raw_payload", "error", "updated_at",
        ])
    _push_chat_refresh(message, "sent")
    finalize_hosted_send(account=account, message=message)

    hosted_meta = existing_payload.get("shvya_hosted") or {}
    if hosted_meta.get("origin") == "agent" and message.lead_id:
        from services.channels.hosted_automation_service import register_hosted_manual_outbound

        register_hosted_manual_outbound(
            account=account,
            lead=message.lead,
            at=message.updated_at,
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
