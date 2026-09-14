"""Instagram professional account connection and chat views.

HTTP views stay thin: validate browser input, read tenant-scoped persisted state,
and enqueue external Meta work in Celery.
"""

from __future__ import annotations

from django.contrib import messages
from django.core import signing
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from services.channels.instagram_service import (
    InstagramAPIError,
    account_sync_is_stale,
    build_authorize_url,
    connection_dict,
    create_oauth_attempt,
    get_account,
    get_conversation,
    instagram_app_id,
    mark_conversation_read,
    meta_credentials_available,
    list_conversations,
    queue_text_message,
)

from .instagram_models import InstagramAccount, InstagramOAuthAttempt
from .instagram_tasks import (
    complete_instagram_oauth_task,
    disconnect_instagram_account_task,
    send_instagram_message_task,
    sync_instagram_account_task,
)


OAUTH_STATE_SALT = "shvya-instagram-oauth"
OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60


def _connection_context(organization):
    account = get_account(organization)
    latest_attempt = InstagramOAuthAttempt.objects.filter(organization=organization).first()
    return {
        "instagram_connection": connection_dict(account),
        "instagram_connected": bool(account),
        "instagram_connection_error": (
            account.last_error
            if account
            else latest_attempt.error_message
            if latest_attempt and latest_attempt.status == InstagramOAuthAttempt.Status.FAILED
            else ""
        ),
        "instagram_meta_ready": meta_credentials_available(),
        "instagram_oauth_pending": bool(
            latest_attempt
            and latest_attempt.status
            in {
                InstagramOAuthAttempt.Status.QUEUED,
                InstagramOAuthAttempt.Status.PROCESSING,
            }
        ),
    }


@crm_login_required
@require_GET
def instagram_connect_view(request):
    return render(
        request,
        "channels/instagram_connect.html",
        _connection_context(request.crm_user.organization),
    )


@crm_login_required
@require_GET
def instagram_oauth_start_view(request):
    user = request.crm_user
    if not meta_credentials_available():
        messages.error(request, "Meta app credentials are not configured for Instagram yet.")
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
    return redirect(
        build_authorize_url(
            app_id=instagram_app_id(),
            redirect_uri=redirect_uri,
            state=state,
        )
    )


@crm_login_required
@require_GET
def instagram_oauth_return_view(request):
    user = request.crm_user
    error = (request.GET.get("error") or "").strip()
    if error:
        description = (
            request.GET.get("error_description")
            or request.GET.get("error_reason")
            or error
        )
        messages.error(request, f"Instagram connection was cancelled: {description}")
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
    attempt = create_oauth_attempt(
        organization=user.organization,
        user=user,
        code=code,
        redirect_uri=redirect_uri,
    )
    transaction.on_commit(
        lambda attempt_id=str(attempt.id): complete_instagram_oauth_task.delay(attempt_id)
    )
    messages.success(
        request,
        "Instagram authorized. SHVYA is securely finishing the connection and syncing your inbox.",
    )
    return redirect("crm-instagram-connect")


@crm_login_required
@require_POST
def instagram_disconnect_view(request):
    account = get_account(request.crm_user.organization)
    if account:
        account.status = InstagramAccount.Status.DISCONNECTED
        account.save(update_fields=["status", "updated_at"])
        transaction.on_commit(
            lambda account_id=str(account.id): disconnect_instagram_account_task.delay(account_id)
        )
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
    account = get_account(organization)
    if not account:
        return None

    if account_sync_is_stale(account):
        sync_instagram_account_task.delay(str(account.id))

    query = (request.GET.get("q") or "").strip()
    conversations = _conversation_search(list_conversations(organization), query)
    return {
        "instagram_connection": connection_dict(account),
        "conversations": conversations,
        "active_conversation": active_conversation,
        "search_query": query,
        "instagram_api_error": account.last_error,
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
        mark_conversation_read(organization, conversation_id)
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
    try:
        queued = queue_text_message(
            organization,
            conversation_id=conversation_id,
            body=body,
        )
    except InstagramAPIError as exc:
        messages.error(request, str(exc))
    else:
        transaction.on_commit(
            lambda message_id=str(queued.id): send_instagram_message_task.delay(message_id)
        )
        messages.success(request, "Message queued for Instagram.")

    return redirect("crm-instagram-chat-detail", conversation_id=conversation_id)
