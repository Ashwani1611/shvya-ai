"""HTTP/UI endpoints for hosted WhatsApp linked-device sessions."""

import json

from decouple import config
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.crm.decorators import crm_login_required
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    create_hosted_account,
    get_pipeline_for_account,
    get_session_settings,
    handle_gateway_event,
    queue_hosted_text_message,
    queued_messages,
    update_session_settings,
)

from .hosted_tasks import (
    initialize_hosted_session_task,
    logout_hosted_session_task,
    refresh_hosted_qr_task,
    sync_hosted_history_task,
)
from .models import WhatsAppAccount, WhatsAppMessage
from .providers.whatsapp_web import WhatsAppWebClient, WhatsAppWebGatewayError
from .tasks import send_whatsapp_message_task


SESSION_LABELS = {
    "initializing": "Connecting",
    "qr_ready": "QR Ready",
    "connecting": "Connecting",
    "syncing": "Syncing",
    "running": "Running",
    "disconnected": "Disconnected",
    "failed": "Failed",
}


def _organization(request):
    user = getattr(request, "crm_user", None)
    organization = getattr(user, "organization", None)
    if not organization:
        raise Http404("Organization not found.")
    return organization


def _hosted_account(request, account_id):
    return WhatsAppAccount.objects.select_related("organization").filter(
        id=account_id,
        organization=_organization(request),
        connection_type="hosted",
        is_active=True,
    ).first()


def _payload(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
    return request.POST.dict()


def _initial_status(account):
    if account.status == WhatsAppAccount.Status.CONNECTED:
        return "Running"
    if account.status == WhatsAppAccount.Status.FAILED:
        return "Failed"
    if account.status == WhatsAppAccount.Status.DISCONNECTED:
        return "Disconnected"
    return "QR Ready"


def _reconcile_gateway_status(account, result):
    """Keep the database status aligned with the live gateway session state."""
    raw_status = str(result.get("status") or "initializing").lower()
    phone_number = result.get("phoneNumber") or account.display_phone_number

    if raw_status == "running":
        handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": "ready",
                "phoneNumber": phone_number,
            }
        )
    elif raw_status in {"failed", "disconnected"}:
        handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": raw_status,
            }
        )
    elif raw_status in {"initializing", "qr_ready", "connecting", "syncing"}:
        handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": "syncing" if raw_status == "syncing" else "connecting",
            }
        )

    account.refresh_from_db()
    return raw_status


@crm_login_required
def whatsapp_connect_hosted_view(request):
    organization = _organization(request)
    accounts = list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            connection_type="hosted",
            is_active=True,
        ).order_by("-connected_at")
    )

    rows = []
    for account in accounts:
        pipeline = get_pipeline_for_account(account=account)
        owner = pipeline.owner if pipeline else None
        rows.append(
            {
                "account": account,
                "pipeline": pipeline,
                "pipeline_owner": owner,
                "status_label": _initial_status(account),
            }
        )

    return render(
        request,
        "channels/whatsapp_connect_hosted.html",
        {
            "organization": organization,
            "hosted_rows": rows,
        },
    )


@crm_login_required
@require_POST
def hosted_session_create_view(request):
    data = _payload(request)
    try:
        account, pipeline, created = create_hosted_account(
            organization=_organization(request),
            created_by=request.crm_user,
            country_code=data.get("country_code", "+91"),
            phone_number=data.get("phone_number", ""),
        )
    except HostedWhatsAppValidationError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    initialize_hosted_session_task.delay(str(account.id))
    return JsonResponse(
        {
            "ok": True,
            "created": created,
            "account_id": str(account.id),
            "phone_number": account.display_phone_number,
            "pipeline": pipeline.name,
            "status": "Connecting",
        },
        status=201 if created else 200,
    )


@crm_login_required
@require_GET
def hosted_session_status_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404
    try:
        result = WhatsAppWebClient().get_session(session_id=account.id)
        raw_status = _reconcile_gateway_status(account, result)
        return JsonResponse(
            {
                "ok": True,
                "status": raw_status,
                "label": SESSION_LABELS.get(
                    raw_status,
                    raw_status.replace("_", " ").title(),
                ),
                "phone_number": result.get("phoneNumber") or account.display_phone_number,
            }
        )
    except WhatsAppWebGatewayError as exc:
        return JsonResponse(
            {
                "ok": False,
                "status": account.status,
                "label": _initial_status(account),
                "error": str(exc),
            },
            status=503,
        )


@crm_login_required
@require_GET
def hosted_session_qr_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404
    try:
        result = WhatsAppWebClient().get_qr(session_id=account.id)
    except WhatsAppWebGatewayError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    return JsonResponse(
        {
            "ok": True,
            "status": result.get("status"),
            "label": SESSION_LABELS.get(
                str(result.get("status") or ""),
                str(result.get("status") or "").replace("_", " ").title(),
            ),
            "qr": result.get("qr"),
            "expires_in": result.get("expiresIn", 60),
        }
    )


@crm_login_required
@require_POST
def hosted_session_qr_refresh_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404
    refresh_hosted_qr_task.delay(str(account.id))
    return JsonResponse({"ok": True, "status": "Connecting"})


@crm_login_required
@require_http_methods(["GET", "POST"])
def hosted_session_settings_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404

    if request.method == "GET":
        return JsonResponse(
            {
                "ok": True,
                "phone_number": account.display_phone_number,
                "settings": get_session_settings(account=account),
            }
        )

    try:
        settings = update_session_settings(account=account, payload=_payload(request))
    except HostedWhatsAppValidationError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "settings": settings})


@crm_login_required
@require_GET
def hosted_session_queue_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404
    items = [
        {
            "id": str(message.id),
            "to": message.to_number,
            "lead": message.lead.name if message.lead else "",
            "body": message.body,
            "message_type": message.message_type,
            "created_at": message.created_at.isoformat(),
            "origin": (message.raw_payload or {}).get("shvya_hosted", {}).get(
                "origin",
                (message.raw_payload or {}).get("shvya_ai", {}).get(
                    "origin",
                    "Queued message",
                ),
            ),
        }
        for message in queued_messages(account=account)[:200]
    ]
    return JsonResponse(
        {
            "ok": True,
            "phone_number": account.display_phone_number,
            "items": items,
        }
    )


@crm_login_required
@require_POST
def hosted_session_logout_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404
    logout_hosted_session_task.delay(str(account.id))
    account.status = WhatsAppAccount.Status.DISCONNECTED
    account.save(update_fields=["status", "updated_at"])
    return JsonResponse({"ok": True, "status": "Disconnected"})


def _chat_key(message):
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    if payload.get("isGroup"):
        return payload.get("chatId") or message.from_number
    if message.direction == WhatsAppMessage.Direction.INBOUND:
        return message.from_number
    return message.to_number


def _chat_name(message):
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    return (
        payload.get("chatName")
        or payload.get("contactName")
        or (message.lead.name if message.lead else "")
        or _chat_key(message)
    )


@crm_login_required
@require_GET
def hosted_session_chats_view(request, account_id):
    account = _hosted_account(request, account_id)
    if not account:
        raise Http404

    recent_messages = list(
        WhatsAppMessage.objects.filter(
            organization=account.organization,
            account=account,
        )
        .select_related("lead")
        .order_by("-created_at")[:500]
    )

    # An empty inbox should repair itself. This covers sessions that were
    # paired successfully while a gateway callback was temporarily lost.
    sync_requested = False
    if not recent_messages:
        sync_hosted_history_task.delay(str(account.id))
        sync_requested = True

    conversations = {}
    for message in recent_messages:
        key = _chat_key(message)
        if not key:
            continue
        if key not in conversations:
            conversations[key] = {
                "key": key,
                "name": _chat_name(message),
                "last_message": message.body or message.get_message_type_display(),
                "last_at": message.created_at,
                "unread": 0,
            }
        if (
            message.direction == WhatsAppMessage.Direction.INBOUND
            and not message.is_read
        ):
            conversations[key]["unread"] += 1

    conversation_rows = list(conversations.values())
    selected = request.GET.get("chat") or (
        conversation_rows[0]["key"] if conversation_rows else ""
    )
    thread = [
        message for message in reversed(recent_messages) if _chat_key(message) == selected
    ]

    if selected:
        WhatsAppMessage.objects.filter(
            id__in=[m.id for m in thread],
            direction=WhatsAppMessage.Direction.INBOUND,
            is_read=False,
        ).update(is_read=True)

    return render(
        request,
        "channels/hosted_whatsapp_chats.html",
        {
            "account": account,
            "conversations": conversation_rows,
            "selected_chat": selected,
            "selected_name": conversations.get(selected, {}).get("name", selected),
            "thread": thread,
            "sync_requested": sync_requested,
        },
    )


@crm_login_required
@require_POST
def hosted_session_chat_send_view(request, account_id):
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

    lead = None
    normalized_chat = chat
    if "@g.us" not in chat:
        from apps.crm.models import Lead
        from services.channels.hosted_whatsapp_service import normalize_whatsapp_number

        normalized_chat = normalize_whatsapp_number(phone_number=chat)
        if not normalized_chat:
            return JsonResponse(
                {"ok": False, "error": "Invalid chat number."},
                status=400,
            )
        lead = Lead.objects.filter(
            organization=account.organization,
            phone=normalized_chat,
        ).first()

    try:
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
    expected = config("WHATSAPP_WEB_CALLBACK_TOKEN", default="")
    supplied = request.headers.get("X-SHVYA-Hosted-Token", "")
    if not expected or not constant_time_compare(expected, supplied):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    try:
        result = handle_gateway_event(payload=data)
    except HostedWhatsAppValidationError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)

    return JsonResponse({"ok": True, "handled": result is not None})
