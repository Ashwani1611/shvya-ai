"""Hosted Account manual sends using the whatsapp-web.js transport only."""

import json
import logging

from django.db import transaction
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_chat_service import queue_hosted_chat_refresh
from services.channels.hosted_send_service import queue_hosted_uploaded_media
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    normalize_whatsapp_number,
    queue_hosted_text_message,
)

from .hosted_send_tasks import send_hosted_whatsapp_message_task
from .models import WhatsAppAccount, WhatsAppMessage

logger = logging.getLogger(__name__)


def _organization(request):
    user = getattr(request, "crm_user", None)
    organization = getattr(user, "organization", None)
    if not organization:
        raise Http404("Organization not found.")
    if not is_hosted_account_enabled(organization):
        raise Http404("Hosted Account is not enabled for this organization.")
    return organization


def _hosted_account(request, account_id):
    return WhatsAppAccount.objects.select_related("organization").filter(
        id=account_id,
        organization=_organization(request),
        connection_type="hosted",
        is_active=True,
    ).first()


def _normalize_chat(chat):
    chat = str(chat or "").strip()
    if not chat:
        return ""
    if "@" in chat:
        if chat.endswith("@c.us"):
            return normalize_whatsapp_number(phone_number=chat.split("@", 1)[0])
        if chat.endswith("@g.us") or chat.endswith("@lid"):
            return chat
        return ""
    return normalize_whatsapp_number(phone_number=chat)


def _lead_for_chat(account, chat):
    if not chat.startswith("+"):
        return None
    return Lead.objects.filter(
        organization=account.organization,
        phone=chat,
    ).first()


def _send_manual_response(*, message, chat_key):
    """Try the durable message now; only genuine provider retries use Celery."""
    try:
        result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
    except Exception:
        # The sender persists terminal failures and never blindly resends an
        # uncertain request. Still return its authoritative row to the inbox.
        logger.exception("Hosted manual send failed for %s", message.id)
        result = {"status": "failed", "reason": "send_failed"}
    message.refresh_from_db()
    try:
        queue_hosted_chat_refresh(
            account_id=message.account_id,
            reason=message.status,
            chat_key=chat_key,
        )
    except Exception:
        logger.exception("Could not publish Hosted manual result for %s", message.id)

    sent = message.status in {
        WhatsAppMessage.Status.SENT,
        WhatsAppMessage.Status.DELIVERED,
        WhatsAppMessage.Status.READ,
    }
    retry_scheduled = (
        result.get("status") == "deferred"
        and message.status == WhatsAppMessage.Status.QUEUED
    )
    pending = retry_scheduled or message.status == "sending"
    error = message.error or result.get("error", "")
    if not sent and not pending and not error:
        error = "The message was not sent. Check the Hosted WhatsApp connection."
    payload = {
        "ok": sent or pending,
        "message": {
            "id": str(message.id),
            "body": message.body,
            "message_type": message.message_type,
            "status": message.status,
            "error": error,
        },
        "retry_scheduled": retry_scheduled,
    }
    if retry_scheduled:
        payload["retry_after"] = result.get("retry_after")
    if not payload["ok"]:
        payload["error"] = error
    status = 201 if sent else 202 if pending else 502
    if result.get("reason") == "session_not_connected":
        status = 409
    return JsonResponse(payload, status=status)


@transaction.non_atomic_requests
@crm_login_required
@require_POST
def hosted_session_chat_send_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404

    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        data = request.POST.dict()
    if not isinstance(data, dict):
        return JsonResponse({"ok": False, "error": "Invalid message payload."}, status=400)

    chat = str(data.get("chat") or "").strip()
    body = str(data.get("body") or "").strip()
    normalized_chat = _normalize_chat(chat)
    if not normalized_chat or not body:
        return JsonResponse(
            {"ok": False, "error": "Chat and message are required."},
            status=400,
        )

    lead = _lead_for_chat(account, normalized_chat)
    try:
        if normalized_chat.endswith("@lid"):
            message = WhatsAppMessage.objects.create(
                organization=account.organization,
                account=account,
                lead=None,
                direction=WhatsAppMessage.Direction.OUTBOUND,
                from_number=account.display_phone_number or account.phone_number_id,
                to_number=normalized_chat,
                body=body,
                message_type=WhatsAppMessage.MessageType.TEXT,
                status=WhatsAppMessage.Status.QUEUED,
                raw_payload={
                    "peerKey": normalized_chat,
                    "rawChatId": normalized_chat,
                    "shvya_hosted": {"origin": "agent", "chat_id": normalized_chat},
                },
            )
        else:
            message = queue_hosted_text_message(
                account=account,
                to_number=normalized_chat,
                body=body,
                lead=lead,
                metadata={"origin": "agent", "chat_id": chat},
            )
    except HostedWhatsAppValidationError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    return _send_manual_response(message=message, chat_key=normalized_chat)


@transaction.non_atomic_requests
@crm_login_required
@require_POST
def hosted_session_chat_media_send_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404

    chat = str(request.POST.get("chat") or "").strip()
    normalized_chat = _normalize_chat(chat)
    if not normalized_chat:
        return JsonResponse(
            {"ok": False, "error": "Invalid WhatsApp recipient."},
            status=400,
        )

    kind = str(request.POST.get("message_type") or "").strip().lower()
    allowed = {
        "image": WhatsAppMessage.MessageType.IMAGE,
        "video": WhatsAppMessage.MessageType.VIDEO,
        "document": WhatsAppMessage.MessageType.DOCUMENT,
    }
    message_type = allowed.get(kind)
    if not message_type:
        return JsonResponse(
            {"ok": False, "error": "Choose photo, video, or document."},
            status=400,
        )

    try:
        message = queue_hosted_uploaded_media(
            account=account,
            to_number=normalized_chat,
            uploaded_file=request.FILES.get("attachment"),
            message_type=message_type,
            caption=request.POST.get("caption", ""),
            lead=_lead_for_chat(account, normalized_chat),
        )
    except HostedWhatsAppValidationError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    return _send_manual_response(message=message, chat_key=normalized_chat)
