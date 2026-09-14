"""Instagram professional account connection and messaging helpers.

This module intentionally keeps Instagram state separate from WhatsApp. The
connected Instagram identity is stored in Organization.settings so the first
release does not require a schema migration, while the access token remains
encrypted at rest.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.organizations.models import Organization


GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"
OAUTH_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
LONG_LIVED_TOKEN_URL = "https://graph.instagram.com/access_token"
REQUEST_TIMEOUT_SECONDS = 15
INSTAGRAM_SETTINGS_KEY = "instagram"
INSTAGRAM_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_messages",
)


class InstagramAPIError(RuntimeError):
    """Raised when Meta rejects an Instagram API operation."""


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt_token(raw_token: str) -> str:
    if not raw_token:
        return ""
    return _fernet().encrypt(raw_token.encode("utf-8")).decode("ascii")


def _decrypt_token(encrypted_token: str) -> str:
    if not encrypted_token:
        return ""
    try:
        return _fernet().decrypt(encrypted_token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise InstagramAPIError(
            "Stored Instagram credentials could not be decrypted. Reconnect Instagram."
        ) from exc


def _instagram_settings(organization: Organization) -> dict:
    data = dict(organization.settings or {}).get(INSTAGRAM_SETTINGS_KEY) or {}
    return dict(data) if isinstance(data, dict) else {}


def get_connection(organization: Organization, *, include_token: bool = False) -> dict | None:
    """Return the organization's Instagram connection, optionally with raw token."""
    data = _instagram_settings(organization)
    if not data or data.get("status") != "connected" or not data.get("ig_user_id"):
        return None

    connection = {
        key: value
        for key, value in data.items()
        if key != "access_token_encrypted"
    }
    if include_token:
        connection["access_token"] = _decrypt_token(data.get("access_token_encrypted", ""))
    return connection


def has_connection(organization: Organization) -> bool:
    try:
        connection = get_connection(organization)
    except InstagramAPIError:
        return False
    return bool(connection)


@transaction.atomic
def save_connection(
    organization: Organization,
    *,
    access_token: str,
    ig_user_id: str,
    username: str = "",
    display_name: str = "",
    profile_picture_url: str = "",
    expires_in: int | None = None,
) -> dict:
    locked = Organization.objects.select_for_update().get(pk=organization.pk)
    org_settings = dict(locked.settings or {})
    now = timezone.now()
    expires_at = None
    if expires_in:
        expires_at = now + timedelta(seconds=int(expires_in))

    org_settings[INSTAGRAM_SETTINGS_KEY] = {
        "status": "connected",
        "ig_user_id": str(ig_user_id),
        "username": str(username or ""),
        "display_name": str(display_name or ""),
        "profile_picture_url": str(profile_picture_url or ""),
        "access_token_encrypted": _encrypt_token(access_token),
        "connected_at": now.isoformat(),
        "token_expires_at": expires_at.isoformat() if expires_at else "",
        "api_version": GRAPH_API_VERSION,
    }
    locked.settings = org_settings
    locked.save(update_fields=["settings", "updated_at"])
    organization.settings = locked.settings
    return get_connection(organization) or {}


@transaction.atomic
def disconnect(organization: Organization) -> None:
    locked = Organization.objects.select_for_update().get(pk=organization.pk)
    org_settings = dict(locked.settings or {})
    org_settings.pop(INSTAGRAM_SETTINGS_KEY, None)
    locked.settings = org_settings
    locked.save(update_fields=["settings", "updated_at"])
    organization.settings = locked.settings


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


def _raise_for_meta(response: requests.Response, label: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.ok:
        return payload

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        detail = error.get("message") or error.get("error_user_msg")
    else:
        detail = payload.get("error_message") if isinstance(payload, dict) else None
    detail = detail or response.text[:300] or f"HTTP {response.status_code}"
    raise InstagramAPIError(f"{label}: {detail}")


def exchange_code_for_connection(
    *,
    organization: Organization,
    code: str,
    redirect_uri: str,
    app_id: str,
    app_secret: str,
) -> dict:
    """Exchange Instagram Login code, upgrade the token, and store profile data."""
    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise InstagramAPIError(f"Instagram login token exchange failed: {exc}") from exc

    short_payload = _raise_for_meta(response, "Instagram login failed")
    short_token = short_payload.get("access_token")
    if not short_token:
        raise InstagramAPIError("Instagram login did not return an access token.")

    try:
        response = requests.get(
            LONG_LIVED_TOKEN_URL,
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": app_secret,
                "access_token": short_token,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise InstagramAPIError(f"Instagram long-lived token exchange failed: {exc}") from exc

    long_payload = _raise_for_meta(response, "Instagram token upgrade failed")
    access_token = long_payload.get("access_token") or short_token
    expires_in = long_payload.get("expires_in")

    profile = _graph_get(
        "me",
        access_token=access_token,
        params={"fields": "id,username,name,profile_picture_url"},
    )
    ig_user_id = profile.get("id") or short_payload.get("user_id")
    if not ig_user_id:
        raise InstagramAPIError("Instagram profile ID was not returned by Meta.")

    return save_connection(
        organization,
        access_token=access_token,
        ig_user_id=str(ig_user_id),
        username=profile.get("username", ""),
        display_name=profile.get("name", ""),
        profile_picture_url=profile.get("profile_picture_url", ""),
        expires_in=expires_in,
    )


def _graph_get(path: str, *, access_token: str, params: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(
            f"{GRAPH_API_BASE}/{path.lstrip('/')}",
            params=params or {},
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise InstagramAPIError(f"Instagram API request failed: {exc}") from exc
    return _raise_for_meta(response, "Instagram API request failed")


def _graph_post(path: str, *, access_token: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            f"{GRAPH_API_BASE}/{path.lstrip('/')}",
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise InstagramAPIError(f"Instagram API request failed: {exc}") from exc
    return _raise_for_meta(response, "Instagram API request failed")


def _other_participant(participants: list[dict], ig_user_id: str) -> dict:
    for participant in participants:
        if str(participant.get("id", "")) != str(ig_user_id):
            return participant
    return participants[0] if participants else {}


def _messages_from_payload(payload: dict) -> list[dict]:
    messages = payload.get("messages") or {}
    items = messages.get("data") if isinstance(messages, dict) else []
    return list(items or [])


def list_conversations(organization: Organization) -> list[dict]:
    connection = get_connection(organization, include_token=True)
    if not connection:
        return []

    payload = _graph_get(
        f"{connection['ig_user_id']}/conversations",
        access_token=connection["access_token"],
        params={
            "fields": (
                "id,updated_time,participants,"
                "messages.limit(1){id,created_time,from,to,message}"
            ),
            "limit": 50,
        },
    )

    conversations = []
    for item in payload.get("data", []) or []:
        participants_data = item.get("participants") or {}
        participants = participants_data.get("data", []) if isinstance(participants_data, dict) else []
        participant = _other_participant(participants, connection["ig_user_id"])
        latest_messages = _messages_from_payload(item)
        latest = latest_messages[0] if latest_messages else {}
        sender = latest.get("from") or {}
        direction = (
            "outbound"
            if str(sender.get("id", "")) == str(connection["ig_user_id"])
            else "inbound"
        )
        conversations.append(
            {
                "id": item.get("id", ""),
                "updated_time": item.get("updated_time", ""),
                "participant_id": str(participant.get("id", "")),
                "participant_username": participant.get("username") or participant.get("name") or "Instagram user",
                "participant_name": participant.get("name") or participant.get("username") or "Instagram user",
                "last_message": latest.get("message", ""),
                "last_message_at": latest.get("created_time") or item.get("updated_time", ""),
                "last_direction": direction,
            }
        )
    return conversations


def get_conversation(organization: Organization, conversation_id: str) -> dict:
    connection = get_connection(organization, include_token=True)
    if not connection:
        raise InstagramAPIError("Instagram is not connected.")

    payload = _graph_get(
        conversation_id,
        access_token=connection["access_token"],
        params={
            "fields": (
                "id,updated_time,participants,"
                "messages.limit(50){id,created_time,from,to,message,attachments}"
            )
        },
    )
    participants_data = payload.get("participants") or {}
    participants = participants_data.get("data", []) if isinstance(participants_data, dict) else []
    participant = _other_participant(participants, connection["ig_user_id"])

    messages = []
    for item in _messages_from_payload(payload):
        sender = item.get("from") or {}
        messages.append(
            {
                "id": item.get("id", ""),
                "body": item.get("message", ""),
                "created_time": item.get("created_time", ""),
                "direction": (
                    "outbound"
                    if str(sender.get("id", "")) == str(connection["ig_user_id"])
                    else "inbound"
                ),
                "attachments": item.get("attachments") or {},
            }
        )
    messages.sort(key=lambda message: message.get("created_time") or "")

    return {
        "id": payload.get("id") or conversation_id,
        "participant_id": str(participant.get("id", "")),
        "participant_username": participant.get("username") or participant.get("name") or "Instagram user",
        "participant_name": participant.get("name") or participant.get("username") or "Instagram user",
        "messages": messages,
    }


def send_text_message(
    organization: Organization,
    *,
    recipient_id: str,
    body: str,
) -> dict:
    connection = get_connection(organization, include_token=True)
    if not connection:
        raise InstagramAPIError("Instagram is not connected.")
    if not recipient_id:
        raise InstagramAPIError("Instagram recipient is missing.")
    body = str(body or "").strip()
    if not body:
        raise InstagramAPIError("Message cannot be empty.")

    return _graph_post(
        f"{connection['ig_user_id']}/messages",
        access_token=connection["access_token"],
        payload={
            "recipient": {"id": recipient_id},
            "message": {"text": body},
        },
    )
