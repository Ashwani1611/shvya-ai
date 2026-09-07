"""Hosted Account send endpoints using the whatsapp-web.js transport only."""

import json

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

    send_hosted_whatsapp_message_task.delay(str(message.id))
    queue_hosted_chat_refresh(
        account_id=account.id,
        reason="queued",
        chat_key=normalized_chat,
    )
    return JsonResponse(
        {
            "ok": True,
            "message": {
                "id": str(message.id),
                "body": message.body,
                "status": message.status,
            },
        },
        status=201,
    )


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

    send_hosted_whatsapp_message_task.delay(str(message.id))
    queue_hosted_chat_refresh(
        account_id=account.id,
        reason="queued_media",
        chat_key=normalized_chat,
    )
    return JsonResponse(
        {
            "ok": True,
            "message": {
                "id": str(message.id),
                "message_type": message.message_type,
                "status": message.status,
            },
        },
        status=201,
    )
