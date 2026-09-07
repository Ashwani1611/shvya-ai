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
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_chat_service import (
    build_hosted_chat_snapshot,
    handle_hosted_gateway_event,
    serialize_hosted_chat_snapshot,
)
from services.channels.hosted_whatsapp_service import handle_gateway_event

from .hosted_tasks import sync_hosted_history_task
from .models import WhatsAppAccount, WhatsAppMessage
from .providers.whatsapp_web import WhatsAppWebClient, WhatsAppWebGatewayError


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

    result = handle_hosted_gateway_event(payload=data)
    return JsonResponse({"ok": True, "handled": result is not None})
