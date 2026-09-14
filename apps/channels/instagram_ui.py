"""Instagram professional account connection and chat views."""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from services.channels.instagram_service import (
    InstagramAPIError,
    build_authorize_url,
    disconnect,
    exchange_code_for_connection,
    get_connection,
    get_conversation,
    list_conversations,
    send_text_message,
)


OAUTH_STATE_SALT = "shvya-instagram-oauth"
OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60


def _meta_credentials_available() -> bool:
    return bool(
        str(getattr(settings, "META_APP_ID", "") or "").strip()
        and str(getattr(settings, "META_APP_SECRET", "") or "").strip()
    )


def _connection_context(organization):
    try:
        connection = get_connection(organization)
        connection_error = ""
    except InstagramAPIError as exc:
        connection = None
        connection_error = str(exc)
    return {
        "instagram_connection": connection,
        "instagram_connected": bool(connection),
        "instagram_connection_error": connection_error,
        "instagram_meta_ready": _meta_credentials_available(),
    }


@crm_login_required
@require_GET
def instagram_connect_view(request):
    context = _connection_context(request.crm_user.organization)
    return render(request, "channels/instagram_connect.html", context)


@crm_login_required
@require_GET
def instagram_oauth_start_view(request):
    user = request.crm_user
    if not _meta_credentials_available():
        messages.error(
            request,
            "Meta app credentials are not configured for Instagram yet.",
        )
        return redirect("crm-instagram-connect")

    redirect_uri = request.build_absolute_uri(reverse("crm-instagram-oauth-return"))
    state = signing.dumps(
        {
            "organization_id": str(user.organization_id),
            "user_id": str(user.id),
        },
        salt=OAUTH_STATE_SALT,
        compress=True,
    )
    authorize_url = build_authorize_url(
        app_id=str(settings.META_APP_ID),
        redirect_uri=redirect_uri,
        state=state,
    )
    return redirect(authorize_url)


@crm_login_required
@require_GET
def instagram_oauth_return_view(request):
    user = request.crm_user
    error = (request.GET.get("error") or "").strip()
    if error:
        error_description = (
            request.GET.get("error_description")
            or request.GET.get("error_reason")
            or error
        )
        messages.error(request, f"Instagram connection was cancelled: {error_description}")
        return redirect("crm-instagram-connect")

    state = (request.GET.get("state") or "").strip()
    code = (request.GET.get("code") or "").strip()
    if not state or not code:
        messages.error(request, "Instagram did not return a valid authorization response.")
        return redirect("crm-instagram-connect")

    try:
        state_data = signing.loads(
            state,
            salt=OAUTH_STATE_SALT,
            max_age=OAUTH_STATE_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        messages.error(request, "Instagram authorization expired or could not be verified.")
        return redirect("crm-instagram-connect")

    if (
        str(state_data.get("organization_id")) != str(user.organization_id)
        or str(state_data.get("user_id")) != str(user.id)
    ):
        messages.error(request, "Instagram authorization does not belong to this workspace.")
        return redirect("crm-instagram-connect")

    redirect_uri = request.build_absolute_uri(reverse("crm-instagram-oauth-return"))
    try:
        connection = exchange_code_for_connection(
            organization=user.organization,
            code=code,
            redirect_uri=redirect_uri,
            app_id=str(settings.META_APP_ID),
            app_secret=str(settings.META_APP_SECRET),
        )
    except InstagramAPIError as exc:
        messages.error(request, str(exc))
        return redirect("crm-instagram-connect")

    username = connection.get("username") or "Instagram"
    messages.success(request, f"@{username} is now connected to SHVYA.")
    return redirect("crm-instagram-chats")


@crm_login_required
@require_POST
def instagram_disconnect_view(request):
    disconnect(request.crm_user.organization)
    messages.success(request, "Instagram was disconnected from this workspace.")
    return redirect("crm-instagram-connect")


def _conversation_search(conversations, query):
    if not query:
        return conversations
    needle = query.casefold()
    return [
        conversation
        for conversation in conversations
        if needle in (conversation.get("participant_name") or "").casefold()
        or needle in (conversation.get("participant_username") or "").casefold()
        or needle in (conversation.get("last_message") or "").casefold()
    ]


def _chat_context(request, *, active_conversation=None):
    organization = request.crm_user.organization
    connection = get_connection(organization)
    if not connection:
        return None

    api_error = ""
    try:
        conversations = list_conversations(organization)
    except InstagramAPIError as exc:
        conversations = []
        api_error = str(exc)

    query = (request.GET.get("q") or "").strip()
    conversations = _conversation_search(conversations, query)
    return {
        "instagram_connection": connection,
        "conversations": conversations,
        "active_conversation": active_conversation,
        "search_query": query,
        "instagram_api_error": api_error,
    }


@crm_login_required
@require_GET
def instagram_chat_list_view(request):
    context = _chat_context(request)
    if context is None:
        messages.info(request, "Connect Instagram before opening chats.")
        return redirect("crm-instagram-connect")
    return render(request, "channels/instagram_chat_list.html", context)


@crm_login_required
@require_GET
def instagram_chat_detail_view(request, conversation_id):
    organization = request.crm_user.organization
    try:
        active_conversation = get_conversation(organization, conversation_id)
    except InstagramAPIError as exc:
        messages.error(request, str(exc))
        return redirect("crm-instagram-chats")

    context = _chat_context(request, active_conversation=active_conversation)
    if context is None:
        return redirect("crm-instagram-connect")
    return render(request, "channels/instagram_chat_list.html", context)


@crm_login_required
@require_POST
def instagram_send_message_view(request, conversation_id):
    organization = request.crm_user.organization
    body = (request.POST.get("body") or "").strip()
    if not body:
        messages.error(request, "Type a message before sending.")
        return redirect("crm-instagram-chat-detail", conversation_id=conversation_id)

    try:
        conversation = get_conversation(organization, conversation_id)
        recipient_id = conversation.get("participant_id")
        send_text_message(
            organization,
            recipient_id=recipient_id,
            body=body,
        )
    except InstagramAPIError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Message sent on Instagram.")

    return redirect("crm-instagram-chat-detail", conversation_id=conversation_id)
