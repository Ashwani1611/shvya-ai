"""Production Instagram Login, sync, messaging, and webhook service layer.

Meta's Instagram API is treated as an external source of events, while SHVYA's
PostgreSQL records are the read model used by the dashboard. Views only enqueue
work; network traffic to Meta belongs in Celery tasks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.channels.instagram_models import (
    InstagramAccount,
    InstagramConversation,
    InstagramMessage,
    InstagramOAuthAttempt,
    InstagramWebhookDelivery,
)
from apps.organizations.models import Organization


DEFAULT_GRAPH_API_VERSION = "v26.0"
OAUTH_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
LONG_LIVED_TOKEN_URL = "https://graph.instagram.com/access_token"
REFRESH_TOKEN_URL = "https://graph.instagram.com/refresh_access_token"
REQUEST_TIMEOUT_SECONDS = 20
INSTAGRAM_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_messages",
)
WEBHOOK_FIELDS = ("messages", "messaging_postbacks")
TOKEN_REFRESH_WINDOW_DAYS = 10


class InstagramAPIError(RuntimeError):
    """Safe normalized Meta error with enough metadata for retry decisions."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | str | None = None,
        subcode: int | str | None = None,
        transient: bool = False,
        fbtrace_id: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.subcode = subcode
        self.transient = transient
        self.fbtrace_id = fbtrace_id

    @property
    def token_invalid(self) -> bool:
        return str(self.code or "") == "190"


def graph_api_version() -> str:
    value = str(getattr(settings, "INSTAGRAM_GRAPH_API_VERSION", "") or "").strip()
    return value or DEFAULT_GRAPH_API_VERSION


def graph_api_base() -> str:
    return f"https://graph.instagram.com/{graph_api_version()}"


def instagram_app_id() -> str:
    return str(
        getattr(settings, "META_INSTAGRAM_APP_ID", "")
        or getattr(settings, "META_APP_ID", "")
        or ""
    ).strip()


def instagram_app_secret() -> str:
    return str(
        getattr(settings, "META_INSTAGRAM_APP_SECRET", "")
        or getattr(settings, "META_APP_SECRET", "")
        or ""
    ).strip()


def instagram_verify_token() -> str:
    return str(
        getattr(settings, "META_INSTAGRAM_VERIFY_TOKEN", "")
        or getattr(settings, "META_VERIFY_TOKEN", "")
        or ""
    ).strip()


def meta_credentials_available() -> bool:
    return bool(instagram_app_id() and instagram_app_secret())


def build_authorize_url(*, app_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(INSTAGRAM_SCOPES),
            "state": state,
            "enable_fb_login": "0",
            "force_authentication": "1",
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{query}"


def _response_payload(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _raise_for_meta(response: requests.Response, label: str) -> dict:
    payload = _response_payload(response)
    if response.ok:
        return payload

    error = payload.get("error")
    if isinstance(error, dict):
        detail = error.get("error_user_msg") or error.get("message")
        code = error.get("code")
        subcode = error.get("error_subcode")
        transient = bool(error.get("is_transient"))
        fbtrace_id = str(error.get("fbtrace_id") or "")
    else:
        detail = payload.get("error_message")
        code = payload.get("error_type")
        subcode = None
        transient = response.status_code >= 500
        fbtrace_id = ""

    detail = str(detail or response.text[:300] or f"HTTP {response.status_code}")
    raise InstagramAPIError(
        f"{label}: {detail}",
        status_code=response.status_code,
        code=code,
        subcode=subcode,
        transient=transient or response.status_code >= 500,
        fbtrace_id=fbtrace_id,
    )


def _request(method: str, url: str, *, label: str, **kwargs) -> dict:
    try:
        response = requests.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
    except requests.Timeout as exc:
        raise InstagramAPIError(
            f"{label}: Meta request timed out.", transient=True
        ) from exc
    except requests.RequestException as exc:
        raise InstagramAPIError(
            f"{label}: Meta request failed: {exc}", transient=True
        ) from exc
    return _raise_for_meta(response, label)


def _graph_get(path: str, *, access_token: str, params: dict | None = None) -> dict:
    return _request(
        "GET",
        f"{graph_api_base()}/{path.lstrip('/')}",
        label="Instagram API request failed",
        params=params or {},
        headers={"Authorization": f"Bearer {access_token}"},
    )


def _graph_post(
    path: str,
    *,
    access_token: str,
    payload: dict | None = None,
    params: dict | None = None,
) -> dict:
    return _request(
        "POST",
        f"{graph_api_base()}/{path.lstrip('/')}",
        label="Instagram API request failed",
        params=params or {},
        json=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )


def _graph_delete(path: str, *, access_token: str, params: dict | None = None) -> dict:
    return _request(
        "DELETE",
        f"{graph_api_base()}/{path.lstrip('/')}",
        label="Instagram API request failed",
        params=params or {},
        headers={"Authorization": f"Bearer {access_token}"},
    )


def _parse_meta_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=dt_timezone.utc)
    parsed = parse_datetime(str(value))
    if parsed and not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def get_account(
    organization: Organization,
    *,
    include_disconnected: bool = False,
) -> InstagramAccount | None:
    queryset = InstagramAccount.objects.filter(organization=organization)
    if not include_disconnected:
        queryset = queryset.filter(status=InstagramAccount.Status.CONNECTED)
    return queryset.first()


def connection_dict(account: InstagramAccount | None) -> dict | None:
    if not account:
        return None
    return {
        "id": str(account.id),
        "status": account.status,
        "ig_user_id": account.ig_user_id,
        "username": account.username,
        "display_name": account.display_name,
        "account_type": account.account_type,
        "profile_picture_url": account.profile_picture_url,
        "connected_at": account.connected_at.isoformat() if account.connected_at else "",
        "token_expires_at": account.token_expires_at.isoformat() if account.token_expires_at else "",
        "webhook_subscribed": account.webhook_subscribed,
        "last_sync_at": account.last_sync_at.isoformat() if account.last_sync_at else "",
        "last_error": account.last_error,
        "api_version": graph_api_version(),
    }


def get_connection(organization: Organization, *, include_token: bool = False) -> dict | None:
    """Compatibility facade consumed by the existing templates/tests."""
    account = get_account(organization)
    data = connection_dict(account)
    if data and include_token:
        data["access_token"] = account.access_token
    return data


def has_connection(organization: Organization) -> bool:
    return get_account(organization) is not None


@transaction.atomic
def save_connection(
    organization: Organization,
    *,
    access_token: str,
    ig_user_id: str,
    username: str = "",
    display_name: str = "",
    profile_picture_url: str = "",
    account_type: str = "",
    expires_in: int | None = None,
    connected_by=None,
) -> dict:
    """Persist a validated connection. Kept as a small public test/admin helper."""
    now = timezone.now()
    expires_at = now + timedelta(seconds=int(expires_in)) if expires_in else None
    account, _ = InstagramAccount.objects.update_or_create(
        organization=organization,
        defaults={
            "ig_user_id": str(ig_user_id),
            "username": str(username or ""),
            "display_name": str(display_name or ""),
            "account_type": str(account_type or ""),
            "profile_picture_url": str(profile_picture_url or ""),
            "access_token": access_token,
            "token_expires_at": expires_at,
            "token_refreshed_at": now,
            "status": InstagramAccount.Status.CONNECTED,
            "connected_by": connected_by,
            "connected_at": now,
            "last_error": "",
        },
    )
    return connection_dict(account) or {}


def disconnect(organization: Organization) -> None:
    """Local-only compatibility helper. Product flow uses the Celery disconnect task."""
    account = get_account(organization, include_disconnected=True)
    if account:
        account.status = InstagramAccount.Status.DISCONNECTED
        account.access_token = ""
        account.webhook_subscribed = False
        account.subscribed_fields = []
        account.save(
            update_fields=[
                "status",
                "access_token",
                "webhook_subscribed",
                "subscribed_fields",
                "updated_at",
            ]
        )


def create_oauth_attempt(*, organization, user, code: str, redirect_uri: str) -> InstagramOAuthAttempt:
    return InstagramOAuthAttempt.objects.create(
        organization=organization,
        created_by=user,
        authorization_code=code,
        redirect_uri=redirect_uri,
        status=InstagramOAuthAttempt.Status.QUEUED,
        expires_at=timezone.now() + timedelta(minutes=10),
    )


def _exchange_authorization_code(*, code: str, redirect_uri: str) -> tuple[str, int | None, dict]:
    app_id = instagram_app_id()
    app_secret = instagram_app_secret()
    if not app_id or not app_secret:
        raise InstagramAPIError("Instagram Meta app credentials are not configured.")

    short_payload = _request(
        "POST",
        OAUTH_TOKEN_URL,
        label="Instagram login failed",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    short_token = str(short_payload.get("access_token") or "").strip()
    if not short_token:
        raise InstagramAPIError("Instagram login did not return an access token.")

    long_payload = _request(
        "GET",
        LONG_LIVED_TOKEN_URL,
        label="Instagram token upgrade failed",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
    )
    access_token = str(long_payload.get("access_token") or short_token)
    expires_in = long_payload.get("expires_in")
    return access_token, int(expires_in) if expires_in else None, short_payload


def _fetch_profile(access_token: str, short_payload: dict | None = None) -> dict:
    short_payload = short_payload or {}
    try:
        profile = _graph_get(
            "me",
            access_token=access_token,
            params={"fields": "id,user_id,username,name,account_type,profile_picture_url"},
        )
    except InstagramAPIError as exc:
        # Some app/account combinations expose a narrower profile field set.
        if exc.status_code != 400:
            raise
        profile = _graph_get(
            "me",
            access_token=access_token,
            params={"fields": "id,user_id,username"},
        )
    ig_user_id = profile.get("id") or profile.get("user_id") or short_payload.get("user_id")
    if not ig_user_id:
        raise InstagramAPIError("Instagram profile ID was not returned by Meta.")
    profile["resolved_ig_user_id"] = str(ig_user_id)
    return profile


def subscribe_account_webhooks(account: InstagramAccount) -> dict:
    payload = _graph_post(
        f"{account.ig_user_id}/subscribed_apps",
        access_token=account.access_token,
        params={"subscribed_fields": ",".join(WEBHOOK_FIELDS)},
    )
    account.webhook_subscribed = bool(payload.get("success", True))
    account.subscribed_fields = list(WEBHOOK_FIELDS)
    account.last_error = ""
    account.save(
        update_fields=["webhook_subscribed", "subscribed_fields", "last_error", "updated_at"]
    )
    return payload


def unsubscribe_account_webhooks(account: InstagramAccount) -> None:
    if not account.access_token:
        return
    _graph_delete(
        f"{account.ig_user_id}/subscribed_apps",
        access_token=account.access_token,
    )


@transaction.atomic
def complete_oauth_attempt(attempt: InstagramOAuthAttempt) -> InstagramAccount:
    locked = InstagramOAuthAttempt.objects.select_for_update().select_related(
        "organization", "created_by"
    ).get(pk=attempt.pk)
    if locked.status == InstagramOAuthAttempt.Status.CONNECTED:
        account = get_account(locked.organization)
        if account:
            return account
    if locked.expires_at and locked.expires_at <= timezone.now():
        raise InstagramAPIError("Instagram authorization expired. Please connect again.")
    code = locked.authorization_code
    if not code:
        raise InstagramAPIError("Instagram authorization code is missing.")

    locked.status = InstagramOAuthAttempt.Status.PROCESSING
    locked.error_message = ""
    locked.save(update_fields=["status", "error_message", "updated_at"])

    access_token, expires_in, short_payload = _exchange_authorization_code(
        code=code,
        redirect_uri=locked.redirect_uri,
    )
    profile = _fetch_profile(access_token, short_payload)
    ig_user_id = profile["resolved_ig_user_id"]

    foreign_owner = InstagramAccount.objects.filter(ig_user_id=ig_user_id).exclude(
        organization=locked.organization
    ).first()
    if foreign_owner:
        raise InstagramAPIError(
            "This Instagram professional account is already connected to another SHVYA workspace."
        )

    now = timezone.now()
    expires_at = now + timedelta(seconds=expires_in) if expires_in else None
    account, _ = InstagramAccount.objects.update_or_create(
        organization=locked.organization,
        defaults={
            "ig_user_id": ig_user_id,
            "username": str(profile.get("username") or ""),
            "display_name": str(profile.get("name") or ""),
            "account_type": str(profile.get("account_type") or ""),
            "profile_picture_url": str(profile.get("profile_picture_url") or ""),
            "access_token": access_token,
            "token_expires_at": expires_at,
            "token_refreshed_at": now,
            "status": InstagramAccount.Status.CONNECTED,
            "connected_by": locked.created_by,
            "connected_at": now,
            "last_error": "",
        },
    )

    # Remove the old JSON proof-of-concept connection if it exists. This keeps
    # credentials in one encrypted model instead of two stores.
    org_settings = dict(locked.organization.settings or {})
    if "instagram" in org_settings:
        org_settings.pop("instagram", None)
        locked.organization.settings = org_settings
        locked.organization.save(update_fields=["settings", "updated_at"])

    # OAuth code is one-time sensitive material. Never retain it after exchange.
    locked.authorization_code = ""
    locked.status = InstagramOAuthAttempt.Status.CONNECTED
    locked.completed_at = now
    locked.save(
        update_fields=["authorization_code", "status", "completed_at", "updated_at"]
    )
    return account


def fail_oauth_attempt(attempt_id, error: Exception) -> None:
    InstagramOAuthAttempt.objects.filter(pk=attempt_id).update(
        status=InstagramOAuthAttempt.Status.FAILED,
        authorization_code="",
        error_message=str(error)[:2000],
        completed_at=timezone.now(),
        updated_at=timezone.now(),
    )


def refresh_account_token(account: InstagramAccount) -> InstagramAccount:
    if not account.access_token:
        raise InstagramAPIError("Instagram access token is missing. Reconnect Instagram.")
    payload = _request(
        "GET",
        REFRESH_TOKEN_URL,
        label="Instagram token refresh failed",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": account.access_token,
        },
    )
    token = str(payload.get("access_token") or account.access_token)
    expires_in = payload.get("expires_in")
    now = timezone.now()
    account.access_token = token
    account.token_refreshed_at = now
    if expires_in:
        account.token_expires_at = now + timedelta(seconds=int(expires_in))
    account.status = InstagramAccount.Status.CONNECTED
    account.last_error = ""
    account.save(
        update_fields=[
            "access_token",
            "token_refreshed_at",
            "token_expires_at",
            "status",
            "last_error",
            "updated_at",
        ]
    )
    return account


def accounts_due_for_token_refresh():
    cutoff = timezone.now() + timedelta(days=TOKEN_REFRESH_WINDOW_DAYS)
    oldest_refresh = timezone.now() - timedelta(hours=24)
    return InstagramAccount.objects.filter(
        status=InstagramAccount.Status.CONNECTED,
        token_expires_at__isnull=False,
        token_expires_at__lte=cutoff,
    ).filter(
        models_q_token_refresh(oldest_refresh)
    )


def models_q_token_refresh(oldest_refresh):
    from django.db.models import Q

    return Q(token_refreshed_at__isnull=True) | Q(token_refreshed_at__lte=oldest_refresh)


def mark_account_error(account: InstagramAccount, exc: InstagramAPIError) -> None:
    if exc.token_invalid:
        account.status = InstagramAccount.Status.EXPIRED
    elif exc.status_code in (401, 403):
        account.status = InstagramAccount.Status.REVOKED
    else:
        account.status = InstagramAccount.Status.ERROR
    account.last_error = str(exc)[:2000]
    account.save(update_fields=["status", "last_error", "updated_at"])


def _other_participant(participants: list[dict], ig_user_id: str) -> dict:
    for participant in participants:
        if str(participant.get("id", "")) != str(ig_user_id):
            return participant
    return participants[0] if participants else {}


def _normalize_attachments(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, list):
            return data
        return [value] if value else []
    return []


def _detect_message_type(item: dict) -> str:
    if item.get("message"):
        return InstagramMessage.MessageType.TEXT
    attachments = _normalize_attachments(item.get("attachments"))
    if attachments:
        first = attachments[0] if isinstance(attachments[0], dict) else {}
        kind = str(first.get("type") or "").lower()
        if kind in {"image", "audio", "video", "share", "sticker"}:
            return kind
    return InstagramMessage.MessageType.UNKNOWN


def _upsert_conversation(
    *,
    account: InstagramAccount,
    participant: dict,
    meta_conversation_id: str = "",
    raw_payload: dict | None = None,
) -> InstagramConversation:
    participant_id = str(participant.get("id") or "").strip()
    if not participant_id:
        raise InstagramAPIError("Instagram conversation participant ID is missing.")

    conversation = None
    if meta_conversation_id:
        conversation = InstagramConversation.objects.filter(
            account=account,
            meta_conversation_id=meta_conversation_id,
        ).first()
    if not conversation:
        conversation = InstagramConversation.objects.filter(
            account=account,
            participant_id=participant_id,
        ).first()

    defaults = {
        "organization": account.organization,
        "participant_username": str(participant.get("username") or ""),
        "participant_name": str(
            participant.get("name") or participant.get("username") or "Instagram user"
        ),
        "participant_profile_picture_url": str(
            participant.get("profile_picture_url") or ""
        ),
        "raw_payload": raw_payload or {},
    }
    if conversation:
        conversation.meta_conversation_id = meta_conversation_id or conversation.meta_conversation_id
        conversation.participant_id = participant_id
        for field, value in defaults.items():
            if field in {"participant_username", "participant_name", "participant_profile_picture_url"}:
                if value:
                    setattr(conversation, field, value)
            else:
                setattr(conversation, field, value)
        conversation.save()
        return conversation

    return InstagramConversation.objects.create(
        account=account,
        participant_id=participant_id,
        meta_conversation_id=meta_conversation_id or None,
        **defaults,
    )


def _upsert_graph_message(
    *,
    account: InstagramAccount,
    conversation: InstagramConversation,
    item: dict,
) -> InstagramMessage | None:
    external_id = str(item.get("id") or "").strip()
    if not external_id:
        return None
    sender = item.get("from") or {}
    recipients = item.get("to") or {}
    recipient_data = recipients.get("data", []) if isinstance(recipients, dict) else []
    sender_id = str(sender.get("id") or "")
    direction = (
        InstagramMessage.Direction.OUTBOUND
        if sender_id == str(account.ig_user_id)
        else InstagramMessage.Direction.INBOUND
    )
    recipient_id = ""
    if recipient_data and isinstance(recipient_data[0], dict):
        recipient_id = str(recipient_data[0].get("id") or "")
    created = _parse_meta_datetime(item.get("created_time")) or timezone.now()

    message, was_created = InstagramMessage.objects.get_or_create(
        external_id=external_id,
        defaults={
            "organization": account.organization,
            "account": account,
            "conversation": conversation,
            "direction": direction,
            "status": (
                InstagramMessage.Status.RECEIVED
                if direction == InstagramMessage.Direction.INBOUND
                else InstagramMessage.Status.SENT
            ),
            "message_type": _detect_message_type(item),
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "body": str(item.get("message") or ""),
            "attachments": _normalize_attachments(item.get("attachments")),
            "raw_payload": item,
            "is_read": direction == InstagramMessage.Direction.OUTBOUND,
            "sent_at": created,
        },
    )
    if not was_created and message.organization_id != account.organization_id:
        raise InstagramAPIError("Instagram message tenant mismatch detected.")
    return message


def sync_account_conversations(account: InstagramAccount, *, max_conversations: int = 100) -> int:
    """Reconcile Meta Conversations API into the local inbox read model."""
    if account.status != InstagramAccount.Status.CONNECTED or not account.access_token:
        return 0

    synced = 0
    after = None
    while synced < max_conversations:
        params = {"fields": "id,updated_time,participants", "limit": min(50, max_conversations - synced)}
        if after:
            params["after"] = after
        payload = _graph_get(
            f"{account.ig_user_id}/conversations",
            access_token=account.access_token,
            params=params,
        )
        data = payload.get("data") or []
        if not isinstance(data, list):
            data = []
        for summary in data:
            if synced >= max_conversations:
                break
            conversation_id = str(summary.get("id") or "")
            if not conversation_id:
                continue
            detail = _graph_get(
                conversation_id,
                access_token=account.access_token,
                params={
                    "fields": (
                        "id,updated_time,participants,"
                        "messages.limit(100){id,created_time,from,to,message,attachments}"
                    )
                },
            )
            participants_data = detail.get("participants") or summary.get("participants") or {}
            participants = (
                participants_data.get("data", []) if isinstance(participants_data, dict) else []
            )
            participant = _other_participant(participants, account.ig_user_id)
            if not participant:
                continue
            with transaction.atomic():
                conversation = _upsert_conversation(
                    account=account,
                    participant=participant,
                    meta_conversation_id=conversation_id,
                    raw_payload=summary,
                )
                messages_data = detail.get("messages") or {}
                items = messages_data.get("data", []) if isinstance(messages_data, dict) else []
                stored = []
                for item in items or []:
                    message = _upsert_graph_message(
                        account=account,
                        conversation=conversation,
                        item=item,
                    )
                    if message:
                        stored.append(message)
                latest = max(
                    stored,
                    key=lambda msg: msg.sent_at or msg.created_at,
                    default=None,
                )
                if latest:
                    conversation.last_message_text = latest.body
                    conversation.last_message_at = latest.sent_at or latest.created_at
                    conversation.last_direction = latest.direction
                else:
                    conversation.last_message_at = _parse_meta_datetime(detail.get("updated_time"))
                conversation.last_synced_at = timezone.now()
                conversation.unread_count = conversation.messages.filter(
                    direction=InstagramMessage.Direction.INBOUND,
                    is_read=False,
                ).count()
                conversation.save()
            synced += 1

        paging = payload.get("paging") or {}
        cursors = paging.get("cursors") or {} if isinstance(paging, dict) else {}
        next_after = cursors.get("after") if isinstance(cursors, dict) else None
        if not data or not next_after or next_after == after:
            break
        after = next_after

    account.last_sync_at = timezone.now()
    account.last_error = ""
    account.save(update_fields=["last_sync_at", "last_error", "updated_at"])
    return synced


def serialize_conversation(conversation: InstagramConversation) -> dict:
    return {
        "id": str(conversation.id),
        "meta_conversation_id": conversation.meta_conversation_id or "",
        "participant_id": conversation.participant_id,
        "participant_username": conversation.participant_username or "instagram_user",
        "participant_name": conversation.participant_name or "Instagram user",
        "participant_profile_picture_url": conversation.participant_profile_picture_url,
        "last_message": conversation.last_message_text,
        "last_message_at": (
            conversation.last_message_at.isoformat() if conversation.last_message_at else ""
        ),
        "last_direction": conversation.last_direction,
        "unread_count": conversation.unread_count,
    }


def list_conversations(organization: Organization) -> list[dict]:
    account = get_account(organization)
    if not account:
        return []
    queryset = InstagramConversation.objects.filter(
        organization=organization,
        account=account,
    ).order_by("-last_message_at", "-updated_at")
    return [serialize_conversation(conversation) for conversation in queryset]


def _get_scoped_conversation(organization: Organization, conversation_id) -> InstagramConversation:
    account = get_account(organization)
    if not account:
        raise InstagramAPIError("Instagram is not connected.")
    queryset = InstagramConversation.objects.filter(organization=organization, account=account)
    try:
        conversation = queryset.get(pk=conversation_id)
    except (InstagramConversation.DoesNotExist, ValueError):
        conversation = queryset.filter(meta_conversation_id=str(conversation_id)).first()
        if not conversation:
            raise InstagramAPIError("Instagram conversation was not found in this workspace.")
    return conversation


def get_conversation(organization: Organization, conversation_id) -> dict:
    conversation = _get_scoped_conversation(organization, conversation_id)
    messages = []
    for item in conversation.messages.filter(organization=organization).order_by("created_at", "id"):
        messages.append(
            {
                "id": str(item.id),
                "external_id": item.external_id or "",
                "body": item.body,
                "created_time": (item.sent_at or item.created_at).isoformat(),
                "direction": item.direction,
                "status": item.status,
                "message_type": item.message_type,
                "attachments": item.attachments,
            }
        )
    data = serialize_conversation(conversation)
    data["messages"] = messages
    return data


def mark_conversation_read(organization: Organization, conversation_id) -> None:
    conversation = _get_scoped_conversation(organization, conversation_id)
    conversation.messages.filter(
        organization=organization,
        direction=InstagramMessage.Direction.INBOUND,
        is_read=False,
    ).update(is_read=True)
    conversation.unread_count = 0
    conversation.save(update_fields=["unread_count", "updated_at"])


@transaction.atomic
def queue_text_message(
    organization: Organization,
    *,
    conversation_id,
    body: str,
) -> InstagramMessage:
    body = str(body or "").strip()
    if not body:
        raise InstagramAPIError("Message cannot be empty.")
    if len(body) > 1000:
        raise InstagramAPIError("Instagram message is too long.")
    conversation = _get_scoped_conversation(organization, conversation_id)
    account = conversation.account
    if not conversation.participant_id:
        raise InstagramAPIError("Instagram recipient is missing.")

    message = InstagramMessage.objects.create(
        organization=organization,
        account=account,
        conversation=conversation,
        direction=InstagramMessage.Direction.OUTBOUND,
        status=InstagramMessage.Status.QUEUED,
        message_type=InstagramMessage.MessageType.TEXT,
        sender_id=account.ig_user_id,
        recipient_id=conversation.participant_id,
        body=body,
        is_read=True,
    )
    conversation.last_message_text = body
    conversation.last_message_at = timezone.now()
    conversation.last_direction = InstagramMessage.Direction.OUTBOUND
    conversation.save(
        update_fields=["last_message_text", "last_message_at", "last_direction", "updated_at"]
    )
    return message


def send_queued_message(message: InstagramMessage) -> InstagramMessage:
    """Deliver one queued row once; callers decide whether a failure is retriable."""
    message = InstagramMessage.objects.select_related("account", "conversation").get(pk=message.pk)
    if message.status == InstagramMessage.Status.SENT and message.external_id:
        return message
    if message.status not in {InstagramMessage.Status.QUEUED, InstagramMessage.Status.FAILED}:
        return message
    account = message.account
    if account.status != InstagramAccount.Status.CONNECTED or not account.access_token:
        raise InstagramAPIError("Instagram is not connected. Reconnect before sending.")

    result = _graph_post(
        f"{account.ig_user_id}/messages",
        access_token=account.access_token,
        payload={
            "recipient": {"id": message.recipient_id},
            "message": {"text": message.body},
        },
    )
    external_id = str(result.get("message_id") or "").strip()
    if not external_id:
        raise InstagramAPIError("Meta accepted the request without returning a message ID.")

    # If an outbound echo reached the webhook first, never violate unique external_id.
    duplicate = InstagramMessage.objects.filter(external_id=external_id).exclude(pk=message.pk).first()
    if duplicate:
        message.delete()
        return duplicate

    message.external_id = external_id
    message.status = InstagramMessage.Status.SENT
    message.sent_at = timezone.now()
    message.error = ""
    message.raw_payload = result
    message.save(
        update_fields=["external_id", "status", "sent_at", "error", "raw_payload", "updated_at"]
    )
    return message


def fail_message(message_id, error: Exception) -> None:
    InstagramMessage.objects.filter(pk=message_id).update(
        status=InstagramMessage.Status.FAILED,
        error=str(error)[:2000],
        updated_at=timezone.now(),
    )


def _webhook_message_type(message_payload: dict) -> str:
    if message_payload.get("text"):
        return InstagramMessage.MessageType.TEXT
    if message_payload.get("attachments"):
        attachments = _normalize_attachments(message_payload.get("attachments"))
        first = attachments[0] if attachments and isinstance(attachments[0], dict) else {}
        kind = str(first.get("type") or "").lower()
        if kind in {"image", "audio", "video", "share", "sticker"}:
            return kind
    return InstagramMessage.MessageType.UNKNOWN


def process_webhook_delivery(delivery: InstagramWebhookDelivery) -> int:
    """Persist signed Meta messaging events exactly once."""
    delivery = InstagramWebhookDelivery.objects.select_for_update().get(pk=delivery.pk)
    if delivery.status in {
        InstagramWebhookDelivery.Status.PROCESSED,
        InstagramWebhookDelivery.Status.IGNORED,
    }:
        return 0
    delivery.status = InstagramWebhookDelivery.Status.PROCESSING
    delivery.error_message = ""
    delivery.save(update_fields=["status", "error_message"])

    payload = delivery.raw_payload or {}
    if payload.get("object") not in {"instagram", None}:
        delivery.status = InstagramWebhookDelivery.Status.IGNORED
        delivery.processed_at = timezone.now()
        delivery.save(update_fields=["status", "processed_at"])
        return 0

    processed = 0
    for entry in payload.get("entry", []) or []:
        own_id = str(entry.get("id") or "")
        account = InstagramAccount.objects.filter(
            ig_user_id=own_id,
            status=InstagramAccount.Status.CONNECTED,
        ).select_related("organization").first()
        if not account:
            continue
        account.last_webhook_at = timezone.now()
        account.save(update_fields=["last_webhook_at", "updated_at"])

        for event in entry.get("messaging", []) or []:
            sender_id = str((event.get("sender") or {}).get("id") or "")
            recipient_id = str((event.get("recipient") or {}).get("id") or "")
            participant_id = recipient_id if sender_id == own_id else sender_id
            if not participant_id or participant_id == own_id:
                continue

            conversation = _upsert_conversation(
                account=account,
                participant={"id": participant_id, "name": "Instagram user"},
                raw_payload={},
            )

            read_event = event.get("read") or event.get("seen")
            if read_event and sender_id != own_id:
                watermark = _parse_meta_datetime(read_event.get("watermark"))
                outbound = conversation.messages.filter(
                    direction=InstagramMessage.Direction.OUTBOUND,
                    status__in=[InstagramMessage.Status.SENT, InstagramMessage.Status.READ],
                )
                if watermark:
                    outbound = outbound.filter(sent_at__lte=watermark)
                outbound.update(status=InstagramMessage.Status.READ, is_read=True)
                processed += 1
                continue

            message_payload = event.get("message") or {}
            postback = event.get("postback") or {}
            external_id = str(
                message_payload.get("mid") or postback.get("mid") or ""
            ).strip()
            if not external_id:
                continue

            direction = (
                InstagramMessage.Direction.OUTBOUND
                if sender_id == own_id
                else InstagramMessage.Direction.INBOUND
            )
            # SHVYA-created outbound rows get their Meta ID from the Send API.
            # Ignore fresh outbound echoes to avoid a race/duplicate row.
            if direction == InstagramMessage.Direction.OUTBOUND:
                existing = InstagramMessage.objects.filter(external_id=external_id).first()
                if existing:
                    existing.status = InstagramMessage.Status.SENT
                    existing.save(update_fields=["status", "updated_at"])
                continue

            body = str(message_payload.get("text") or postback.get("title") or "")
            attachments = _normalize_attachments(message_payload.get("attachments"))
            event_time = _parse_meta_datetime(event.get("timestamp")) or timezone.now()
            try:
                message, created = InstagramMessage.objects.get_or_create(
                    external_id=external_id,
                    defaults={
                        "organization": account.organization,
                        "account": account,
                        "conversation": conversation,
                        "direction": InstagramMessage.Direction.INBOUND,
                        "status": InstagramMessage.Status.RECEIVED,
                        "message_type": (
                            InstagramMessage.MessageType.POSTBACK
                            if postback
                            else _webhook_message_type(message_payload)
                        ),
                        "sender_id": sender_id,
                        "recipient_id": recipient_id,
                        "body": body,
                        "attachments": attachments,
                        "raw_payload": event,
                        "is_read": False,
                        "sent_at": event_time,
                    },
                )
            except IntegrityError:
                message = InstagramMessage.objects.get(external_id=external_id)
                created = False
            if message.organization_id != account.organization_id:
                raise InstagramAPIError("Instagram webhook message tenant mismatch detected.")
            if created:
                conversation.last_message_text = body
                conversation.last_message_at = event_time
                conversation.last_direction = InstagramMessage.Direction.INBOUND
                conversation.unread_count = conversation.messages.filter(
                    direction=InstagramMessage.Direction.INBOUND,
                    is_read=False,
                ).count()
                conversation.save(
                    update_fields=[
                        "last_message_text",
                        "last_message_at",
                        "last_direction",
                        "unread_count",
                        "updated_at",
                    ]
                )
                processed += 1

    delivery.status = (
        InstagramWebhookDelivery.Status.PROCESSED
        if processed
        else InstagramWebhookDelivery.Status.IGNORED
    )
    delivery.processed_at = timezone.now()
    delivery.save(update_fields=["status", "processed_at", "error_message"])
    return processed


def fail_webhook_delivery(delivery_id, error: Exception) -> None:
    InstagramWebhookDelivery.objects.filter(pk=delivery_id).update(
        status=InstagramWebhookDelivery.Status.FAILED,
        error_message=str(error)[:2000],
        processed_at=timezone.now(),
    )


def account_sync_is_stale(account: InstagramAccount, *, seconds: int = 60) -> bool:
    return not account.last_sync_at or account.last_sync_at <= timezone.now() - timedelta(seconds=seconds)
