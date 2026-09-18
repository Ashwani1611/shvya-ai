"""Instagram professional account connection and chat views.

Meta I/O stays in Celery. JSON snapshots use the same authenticated routes and
same tenant boundary as HTML; polling never starts another provider sync.
"""
from __future__ import annotations

import logging
import secrets
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.crm.decorators import crm_login_required
from services.channels import instagram_service as provider
from services.channels.instagram_content import POLICY_URL, safe_url
from services.channels.instagram_inbox import (
    connection_error, inbox_conversations, inbox_thread, mark_loaded_read,
    queue_inbox_reply, serialize_message,
)
from .instagram_models import InstagramAccount, InstagramMessage, InstagramOAuthAttempt
from .instagram_tasks import (
    complete_instagram_oauth_task, disconnect_instagram_account_task,
    send_instagram_message_task, sync_instagram_account_task,
)

logger = logging.getLogger(__name__)
OAUTH_STATE_SALT = "shvya-instagram-oauth"
OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60


def _json_requested(request):
    return "application/json" in request.headers.get("Accept", "")


def _private(response):
    response["Cache-Control"] = "private, no-store"
    patch_vary_headers(response, ["Accept", "Cookie"])
    return response


def _public_uri(request, path):
    value = request.build_absolute_uri(path)
    parts = urlsplit(value)
    if not settings.DEBUG and parts.hostname not in {"localhost", "127.0.0.1", "testserver"}:
        value = urlunsplit(("https", parts.netloc, parts.path, "", ""))
    return value


def _connection_context(organization):
    account = provider.get_account(organization, include_disconnected=True)
    latest = InstagramOAuthAttempt.objects.filter(organization=organization).first()
    now = timezone.now()
    pending = bool(latest and latest.status in (
        InstagramOAuthAttempt.Status.QUEUED, InstagramOAuthAttempt.Status.PROCESSING,
    ))
    expired = bool(pending and latest.expires_at and latest.expires_at <= now)
    connected = bool(account and account.status == InstagramAccount.Status.CONNECTED)
    token_expired = bool(account and account.token_expires_at and account.token_expires_at <= now)
    error = account.last_error if account else ""
    if latest and latest.status == InstagramOAuthAttempt.Status.FAILED:
        error = latest.error_message
    if expired:
        error = "Instagram authorization timed out. Start Connect again; check the OAuth worker if this repeats."
    if token_expired:
        error = "The Instagram access token expired. Reconnect the professional account."
    connection = provider.connection_dict(account)
    if connection:
        connection["last_error"] = connection_error(error, account)
        connection["profile_picture_url"] = safe_url(connection.get("profile_picture_url"))
    ready = bool(connected and not token_expired and account.webhook_subscribed and account.last_sync_at)
    return {
        "instagram_connection": connection,
        "instagram_connected": connected,
        "instagram_connection_error": connection_error(error, account),
        "instagram_meta_ready": provider.meta_credentials_available(),
        "instagram_verify_ready": bool(provider.instagram_verify_token()),
        "instagram_oauth_pending": pending and not expired,
        "instagram_inbox_ready": ready,
        "instagram_connection_state": (
            "authorizing" if pending and not expired else "ready" if ready else
            "authorized_with_warning" if connected and not token_expired else
            "expired" if expired or token_expired else account.status if account else "not_connected"
        ),
    }


def _queue_sync_once(account):
    if not account or account.status != InstagramAccount.Status.CONNECTED:
        return
    key = f"instagram:sync:{account.organization_id}:{account.pk}"
    try:
        if provider.account_sync_is_stale(account) and cache.add(key, "queued", timeout=90):
            try:
                sync_instagram_account_task.delay(str(account.pk))
            except Exception:
                cache.delete(key)
                logger.warning("Instagram sync could not be queued for account %s", account.pk)
    except Exception:
        # Redis errors should not prevent reading already-persisted conversations.
        logger.warning("Instagram sync scheduler unavailable for account %s", account.pk)


@crm_login_required
@require_http_methods(["GET", "POST"])
def instagram_connect_view(request):
    organization = request.crm_user.organization
    if request.method == "POST":
        account = provider.get_account(organization)
        if not account:
            messages.error(request, "Reconnect Instagram before retrying inbox setup.")
        else:
            try:
                sync_instagram_account_task.delay(str(account.pk))
            except Exception:
                messages.error(request, "Inbox setup could not be queued. Check the background worker and broker.")
            else:
                messages.success(request, "Inbox setup retry queued.")
        return redirect("crm-instagram-connect")
    context = _connection_context(organization)
    context.update(
        instagram_redirect_uri=_public_uri(request, reverse("crm-instagram-oauth-return")),
        instagram_webhook_uri=_public_uri(request, "/webhooks/instagram/"),
        instagram_policy_url=POLICY_URL,
    )
    if _json_requested(request):
        return _private(JsonResponse(context))
    return _private(render(request, "channels/instagram_connect.html", context))


@crm_login_required
@require_GET

def instagram_oauth_start_view(request):
    if not provider.meta_credentials_available():
        messages.error(request, "Instagram app credentials are not configured. Check the dedicated Instagram App ID and App Secret.")
        return redirect("crm-instagram-connect")
    redirect_uri = _public_uri(request, reverse("crm-instagram-oauth-return"))
    state = signing.dumps({
        "organization_id": str(request.crm_user.organization_id),
        "user_id": str(request.crm_user.pk),
        "nonce": secrets.token_urlsafe(24), "redirect_uri": redirect_uri,
    }, salt=OAUTH_STATE_SALT, compress=True)
    return redirect(provider.build_authorize_url(
        app_id=provider.instagram_app_id(), redirect_uri=redirect_uri, state=state,
    ))


@crm_login_required
@require_GET

def instagram_oauth_return_view(request):
    state = request.GET.get("state", "")
    code = request.GET.get("code", "")
    try:
        data = signing.loads(state, salt=OAUTH_STATE_SALT, max_age=OAUTH_STATE_MAX_AGE_SECONDS)
        if (not isinstance(data, dict) or
                data.get("organization_id") != str(request.crm_user.organization_id) or
                data.get("user_id") != str(request.crm_user.pk)):
            raise signing.BadSignature
        # Nonce-less states are accepted only for signed, short-lived callbacks
        # already issued by the preceding release during a rolling deployment.
        nonce = data.get("nonce")
        if nonce and not cache.add(
            f"instagram:oauth-used:{request.crm_user.organization_id}:{nonce}", True,
            timeout=OAUTH_STATE_MAX_AGE_SECONDS,
        ):
            raise signing.BadSignature
    except signing.BadSignature:
        messages.error(request, "Instagram authorization is invalid, expired, or already used. Start Connect again.")
        return redirect("crm-instagram-connect")
    except Exception:
        messages.error(request, "Instagram authorization could not be verified. Please try Connect again.")
        return redirect("crm-instagram-connect")
    if request.GET.get("error"):
        messages.error(request, "Instagram authorization was cancelled or permissions were not granted.")
        return redirect("crm-instagram-connect")
    if not code or len(code) > 8192:
        messages.error(request, "Instagram did not return an authorization code. Start Connect again.")
        return redirect("crm-instagram-connect")
    attempt = provider.create_oauth_attempt(
        organization=request.crm_user.organization, user=request.crm_user, code=code,
        redirect_uri=data.get("redirect_uri") or _public_uri(request, reverse("crm-instagram-oauth-return")),
    )
    def enqueue():
        try:
            complete_instagram_oauth_task.delay(str(attempt.pk))
        except Exception:
            provider.fail_oauth_attempt(attempt.pk, "Instagram authorization could not be queued. Check the worker/broker, then Connect again.")
            logger.warning("Instagram OAuth enqueue failed for attempt %s", attempt.pk)
    transaction.on_commit(enqueue)
    messages.info(request, "Instagram authorization received. Connection status is shown below.")
    return redirect("crm-instagram-connect")


@crm_login_required
@require_POST

def instagram_disconnect_view(request):
    account = provider.get_account(request.crm_user.organization, include_disconnected=True)
    if account:
        account.status = InstagramAccount.Status.DISCONNECTED
        account.save(update_fields=["status", "updated_at"])
        def enqueue():
            try:
                disconnect_instagram_account_task.delay(str(account.pk))
            except Exception:
                logger.warning("Instagram disconnect cleanup could not be queued for %s", account.pk)
        transaction.on_commit(enqueue)
    messages.success(request, "Instagram disconnected from this workspace.")
    return redirect("crm-instagram-connect")


def _present_thread(thread, organization):
    from apps.channels.instagram_models import InstagramConversation
    from apps.ai_engagement.services.intent_score import intent_score_for_lead
    conversation = InstagramConversation.objects.select_related("lead").get(pk=thread["id"], organization=organization, account__organization=organization)
    lead = conversation.lead
    thread["lead_url"] = reverse("crm-instagram-link-lead", args=[thread["id"]])
    thread["lead_name"] = lead.name if lead and lead.organization_id == conversation.organization_id else ""
    thread["intent_score"] = intent_score_for_lead(lead=lead) if thread["lead_name"] else None
    thread["url"] = reverse("crm-instagram-chat-detail", args=[thread["id"]])
    thread["send_url"] = reverse("crm-instagram-send-message", args=[thread["id"]])
    for message in thread["messages"]:
        message["html"] = render_to_string("channels/_instagram_message.html", {"message": message})
    return thread


def _chat_response(request, conversation_id=None):
    organization = request.crm_user.organization
    account = provider.get_account(organization, include_disconnected=True)
    if not account:
        if _json_requested(request):
            return _private(JsonResponse({"error": "Connect Instagram to open the inbox."}, status=409))
        return redirect("crm-instagram-connect")
    try:
        offset = int(request.GET.get("offset", 0))
        if offset < 0 or offset > 100000:
            raise ValueError
    except (ValueError, TypeError):
        return _private(JsonResponse({"error": "Invalid conversation page."}, status=400))
    context = _connection_context(organization)
    context.update(inbox_conversations(organization, query=request.GET.get("q", "")[:200], offset=offset))
    context["search_query"] = request.GET.get("q", "")[:200]
    for conversation in context["conversations"]:
        conversation["url"] = reverse("crm-instagram-chat-detail", args=[conversation["id"]])
        conversation["html"] = render_to_string("channels/_instagram_conversation.html", {"conversation": conversation})
    active = None
    if conversation_id:
        try:
            active = _present_thread(inbox_thread(organization, conversation_id, before=request.GET.get("before", "")), organization)
            if not request.GET.get("before"):
                mark_loaded_read(organization, conversation_id, [message["id"] for message in active["messages"]])
        except provider.InstagramAPIError as exc:
            if _json_requested(request):
                return _private(JsonResponse({"error": connection_error(exc, account)}, status=404))
            messages.error(request, connection_error(exc, account))
            return redirect("crm-instagram-chats")
    context.update(active_conversation=active, list_url=reverse("crm-instagram-chats"))
    if _json_requested(request):
        return _private(JsonResponse(context))
    _queue_sync_once(account)
    context["instagram_initial"] = {key: value for key, value in context.items() if key != "instagram_initial"}
    # Reuse the exact WhatsApp API shell, not a second approximated stylesheet.
    from .whatsapp_chat_failure_ui import _inject_chat_ui
    return _private(_inject_chat_ui(render(request, "channels/instagram_chat_list.html", context)))


@crm_login_required
@require_GET

def instagram_chat_list_view(request):
    return _chat_response(request)


@crm_login_required
@require_GET

def instagram_chat_detail_view(request, conversation_id):
    return _chat_response(request, conversation_id)


@crm_login_required
@require_POST

def instagram_send_message_view(request, conversation_id):
    try:
        message = queue_inbox_reply(
            request.crm_user.organization, conversation_id=conversation_id,
            body=request.POST.get("body", ""), idempotency_key=request.POST.get("idempotency_key"),
        )
    except provider.InstagramAPIError as exc:
        if _json_requested(request):
            return _private(JsonResponse({"error": str(exc)}, status=400))
        messages.error(request, str(exc))
        return redirect("crm-instagram-chat-detail", conversation_id=conversation_id)
    errors = []
    if message.status == InstagramMessage.Status.QUEUED:
        def enqueue():
            try:
                send_instagram_message_task.delay(str(message.pk))
            except Exception:
                errors.append("Message saved but could not be queued. Retry the same send request; it will not create a duplicate.")
                logger.warning("Instagram send enqueue failed for message %s", message.pk)
        transaction.on_commit(enqueue)
    if _json_requested(request):
        result = serialize_message(message)
        result["html"] = render_to_string("channels/_instagram_message.html", {"message": result})
        return _private(JsonResponse({"message": result, "error": errors[0] if errors else ""}, status=503 if errors else 202))
    if errors:
        messages.error(request, errors[0])
    return redirect("crm-instagram-chat-detail", conversation_id=conversation_id)
