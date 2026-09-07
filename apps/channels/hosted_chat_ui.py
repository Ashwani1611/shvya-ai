"""WhatsApp-like Hosted Account inbox endpoints.

These views keep the legacy Hosted Account setup/settings URLs untouched while
providing a richer read model for conversations, server-side search, and live
refreshes.
"""

import json

from decouple import config
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_chat_service import (
    build_hosted_chat_snapshot,
    handle_hosted_gateway_event,
    queue_hosted_chat_refresh,
    serialize_hosted_chat_snapshot,
)
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    handle_gateway_event,
    normalize_whatsapp_number,
    queue_hosted_text_message,
)

from .hosted_tasks import sync_hosted_history_task
from .models import WhatsAppAccount, WhatsAppMessage
from .providers.whatsapp_web import WhatsAppWebClient, WhatsAppWebGatewayError
from .tasks import send_whatsapp_message_task


SYNC_THROTTLE_SECONDS = 180


def _organization(request):
    user = getattr(request, "crm_user", None)
    organization = getattr(user, "organization", None)
    if not organization:
        raise Http404("Organization not found.")
    if not is_hosted_account_enabled(organization):
        raise Http404("Hosted Account is not enabled for this organization.")
    return organization


def _hosted_account(request, account_id):
    return (
        WhatsAppAccount.objects.select_related("organization")
        .filter(
            id=account_id,
            organization=_organization(request),
            connection_type="hosted",
            is_active=True,
        )
        .first()
    )


def _payload(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
    return request.POST.dict()


def _repair_live_status(account):
    """Reconcile a restored gateway session before rendering the inbox."""
    try:
        result = WhatsAppWebClient().get_session(session_id=account.id)
    except WhatsAppWebGatewayError:
        return

    if str(result.get("status") or "").lower() == "running":
        handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": "ready",
                "phoneNumber": (
                    result.get("phoneNumber") or account.display_phone_number
                ),
            }
        )
        account.refresh_from_db()


def _request_history_refresh(account):
    """Refresh/enrich history at most once every few minutes per session."""
    if account.status != WhatsAppAccount.Status.CONNECTED:
        return False
    key = f"hosted-chat-history-refresh:{account.id}"
    if not cache.add(key, "1", timeout=SYNC_THROTTLE_SECONDS):
        return False
    sync_hosted_history_task.delay(str(account.id))
    return True


def _mark_thread_read(snapshot):
    thread = snapshot.get("thread") or []
    if not thread:
        return
    ids = [
        message.id
        for message in thread
        if (
            message.direction == WhatsAppMessage.Direction.INBOUND
            and not message.is_read
        )
    ]
    if ids:
        WhatsAppMessage.objects.filter(id__in=ids).update(is_read=True)
    selected = snapshot.get("selected_chat")
    if selected:
        for row in snapshot.get("conversations") or []:
            if row.get("key") == selected:
                row["unread"] = 0
                break


@crm_login_required
@require_GET
def hosted_session_chats_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404

    _repair_live_status(account)
    sync_requested = _request_history_refresh(account)
    snapshot = build_hosted_chat_snapshot(
        account=account,
        selected_chat=request.GET.get("chat", ""),
        query=request.GET.get("q", ""),
    )
    _mark_thread_read(snapshot)

    return render(
        request,
        "channels/hosted_whatsapp_chats.html",
        {
            "account": account,
            "conversations": snapshot["conversations"],
            "selected_chat": snapshot["selected_chat"],
            "selected_name": snapshot["selected_name"],
            "thread": snapshot["thread"],
            "search_query": request.GET.get("q", ""),
            "sync_requested": sync_requested,
        },
    )


@crm_login_required
@require_GET
def hosted_session_chats_data_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404

    snapshot = build_hosted_chat_snapshot(
        account=account,
        selected_chat=request.GET.get("chat", ""),
        query=request.GET.get("q", ""),
    )
    _mark_thread_read(snapshot)
    payload = serialize_hosted_chat_snapshot(snapshot)
    payload.update(
        {
            "ok": True,
            "account_status": account.status,
            "phone_number": account.display_phone_number,
        }
    )
    return JsonResponse(payload)


@crm_login_required
@require_POST
def hosted_session_chat_send_view(request, account_id):
    """Queue a Hosted message using the exact canonical conversation key."""
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404

    data = _payload(request)
    chat = str(data.get("chat") or "").strip()
    body = str(data.get("body") or "").strip()
    if not chat or not body:
        return JsonResponse(
            {"ok": False, "error": "Chat and message are required."},
            status=400,
        )

    raw_whatsapp_id = chat if "@" in chat else ""
    normalized_chat = ""
    lead = None

    if raw_whatsapp_id:
        # Group and LID chat ids are valid whatsapp-web.js destinations. A
        # normal @c.us id is converted to our canonical +digits form so the
        # CRM lead association remains stable.
        if chat.endswith("@c.us"):
            normalized_chat = normalize_whatsapp_number(
                phone_number=chat.split("@", 1)[0]
            )
        elif chat.endswith("@g.us") or chat.endswith("@lid"):
            normalized_chat = chat
        else:
            return JsonResponse(
                {"ok": False, "error": "Unsupported WhatsApp chat id."},
                status=400,
            )
    else:
        normalized_chat = normalize_whatsapp_number(phone_number=chat)

    if not normalized_chat:
        return JsonResponse(
            {"ok": False, "error": "Invalid WhatsApp recipient."},
            status=400,
        )

    if normalized_chat.startswith("+"):
        lead = Lead.objects.filter(
            organization=account.organization,
            phone=normalized_chat,
        ).first()

    try:
        if normalized_chat.endswith("@lid"):
            # A rare fallback for a direct chat whose phone identity has not
            # yet been resolved by WhatsApp. Keep the LID as a transport key;
            # a subsequent history/live payload will repair it to peerPhone.
            message = WhatsAppMessage.objects.create(
                organization=account.organization,
                account=account,
                lead=None,
                direction=WhatsAppMessage.Direction.OUTBOUND,
                from_number=(
                    account.display_phone_number or account.phone_number_id
                ),
                to_number=normalized_chat,
                body=body,
                message_type=WhatsAppMessage.MessageType.TEXT,
                status=WhatsAppMessage.Status.QUEUED,
                raw_payload={
                    "peerKey": normalized_chat,
                    "rawChatId": normalized_chat,
                    "shvya_hosted": {
                        "origin": "agent",
                        "chat_id": normalized_chat,
                    },
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

    send_whatsapp_message_task.delay(str(message.id))
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


@csrf_exempt
@require_POST
def hosted_gateway_event_view(request):
    """Authenticated gateway callback with canonical chat repair + push."""
    expected = config("WHATSAPP_WEB_CALLBACK_TOKEN", default="")
    supplied = request.headers.get("X-SHVYA-Hosted-Token", "")
    if not expected or not constant_time_compare(expected, supplied):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    try:
        result = handle_hosted_gateway_event(payload=data)
    except HostedWhatsAppValidationError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)

    return JsonResponse({"ok": True, "handled": result is not None})
