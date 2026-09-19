from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from apps.integrations.models import (
    DiagnosticOAuthAuthorizationCode,
    DiagnosticOAuthClient,
    DiagnosticOAuthToken,
)
from apps.organizations.access import organization_is_active
from apps.organizations.models import APIKey

DIAGNOSTICS_SCOPE = "diagnostics.read"
OFFLINE_SCOPE = "offline_access"
SUPPORTED_SCOPES = {DIAGNOSTICS_SCOPE, OFFLINE_SCOPE}
ACCESS_TOKEN_TTL = timedelta(hours=8)
REFRESH_TOKEN_TTL = timedelta(days=30)
AUTH_CODE_TTL = timedelta(minutes=5)

_SECRET_KEY_PATTERN = re.compile(
    r"(token|secret|password|credential|authorization|api[_-]?key|cookie|session|"
    r"private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"(?i)\\bBearer\\s+[A-Za-z0-9._~+/=-]{12,}")
_SHVYA_KEY_PATTERN = re.compile(r"\\bshvya_[A-Za-z0-9_-]{12,}")
_LONG_SECRET_PATTERN = re.compile(r"\\b[A-Za-z0-9_-]{40,}\\b")


class DiagnosticAuthError(ValueError):
    pass


def token_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(str(verifier or "").encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sanitize_text(value, *, limit: int = 800, redact_long: bool = True) -> str:
    text = str(value or "")
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _SHVYA_KEY_PATTERN.sub("shvya_[REDACTED]", text)
    if redact_long:
        text = _LONG_SECRET_PATTERN.sub("[REDACTED]", text)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def sanitize_data(value, *, depth: int = 0):
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_PATTERN.search(key_text):
                cleaned[key_text] = "[REDACTED]"
            elif key_text.casefold() in {
                "id",
                "lead_id",
                "message_id",
                "external_id",
                "account_id",
                "organization_id",
                "pipeline_id",
                "stage_id",
                "event_id",
            } and isinstance(item, str):
                cleaned[key_text] = sanitize_text(item, redact_long=False)
            else:
                cleaned[key_text] = sanitize_data(item, depth=depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize_data(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def request_fingerprint(arguments) -> str:
    payload = json.dumps(
        arguments or {},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return token_hash(payload)


def _active_api_key_from_raw(raw_key: str, *, require_diagnostics: bool = True):
    raw_key = str(raw_key or "").strip()
    if not raw_key:
        raise DiagnosticAuthError("Missing diagnostic API key.")

    api_key = (
        APIKey.objects.select_related("organization")
        .filter(key_prefix=raw_key[:16], is_active=True)
        .first()
    )
    if api_key is None or not api_key.verify(raw_key):
        raise DiagnosticAuthError("Invalid diagnostic API key.")
    if not organization_is_active(api_key.organization):
        raise DiagnosticAuthError("Organization account is disabled.")
    if api_key.expires_at and api_key.expires_at <= timezone.now():
        raise DiagnosticAuthError("Diagnostic API key has expired.")
    if require_diagnostics and not getattr(
        api_key,
        "can_read_diagnostics",
        False,
    ):
        raise DiagnosticAuthError(
            "API key is not permitted to read diagnostics."
        )

    now = timezone.now()
    APIKey.objects.filter(pk=api_key.pk).update(last_used_at=now)
    api_key.last_used_at = now
    return api_key


def authenticate_bearer(raw_bearer: str):
    raw_bearer = str(raw_bearer or "").strip()
    if not raw_bearer:
        raise DiagnosticAuthError("Missing bearer token.")

    now = timezone.now()
    oauth_token = (
        DiagnosticOAuthToken.objects.select_related(
            "organization",
            "api_key",
            "client",
        )
        .filter(
            access_token_hash=token_hash(raw_bearer),
            revoked_at__isnull=True,
        )
        .first()
    )
    if oauth_token is not None:
        if oauth_token.expires_at <= now:
            raise DiagnosticAuthError("OAuth access token has expired.")
        if not organization_is_active(oauth_token.organization):
            raise DiagnosticAuthError("Organization account is disabled.")
        if (
            not oauth_token.api_key.is_active
            or not getattr(
                oauth_token.api_key,
                "can_read_diagnostics",
                False,
            )
        ):
            raise DiagnosticAuthError(
                "Diagnostic API key is no longer active."
            )
        scopes = set((oauth_token.scope or "").split())
        if DIAGNOSTICS_SCOPE not in scopes:
            raise DiagnosticAuthError(
                "OAuth token is missing diagnostics.read scope."
            )
        DiagnosticOAuthToken.objects.filter(pk=oauth_token.pk).update(
            last_used_at=now
        )
        return oauth_token.organization, oauth_token.api_key, oauth_token

    api_key = _active_api_key_from_raw(
        raw_bearer,
        require_diagnostics=True,
    )
    return api_key.organization, api_key, None


def register_oauth_client(
    *,
    redirect_uris,
    client_name="",
    grant_types=None,
    response_types=None,
    application_type="web",
):
    redirect_uris = [
        str(item or "").strip()
        for item in (redirect_uris or [])
        if str(item or "").strip()
    ]
    if not redirect_uris:
        raise DiagnosticAuthError(
            "At least one redirect URI is required."
        )

    for uri in redirect_uris:
        parsed = urlparse(uri)
        host = (parsed.hostname or "").lower()
        allowed_host = (
            host == "chatgpt.com"
            or host.endswith(".chatgpt.com")
            or host == "openai.com"
            or host.endswith(".openai.com")
        )
        if parsed.scheme != "https" or not allowed_host:
            raise DiagnosticAuthError(
                "Only HTTPS ChatGPT/OpenAI redirect URIs are accepted."
            )

    grant_types = list(
        grant_types or ["authorization_code", "refresh_token"]
    )
    response_types = list(response_types or ["code"])

    if not set(grant_types) <= {
        "authorization_code",
        "refresh_token",
    }:
        raise DiagnosticAuthError("Unsupported OAuth grant type.")
    if "authorization_code" not in grant_types:
        raise DiagnosticAuthError(
            "authorization_code grant is required."
        )
    if set(response_types) != {"code"}:
        raise DiagnosticAuthError(
            "Only OAuth response_type=code is supported."
        )
    if str(application_type or "web") not in {"web", "native"}:
        raise DiagnosticAuthError(
            "Unsupported OAuth application_type."
        )

    client = DiagnosticOAuthClient.objects.create(
        client_id="shvya_mcp_" + secrets.token_urlsafe(24),
        client_name=str(client_name or "ChatGPT").strip()[:200],
        application_type=str(application_type or "web"),
        redirect_uris=redirect_uris,
        grant_types=grant_types,
        response_types=response_types,
    )
    return client


def validate_authorization_request(
    *,
    client_id,
    redirect_uri,
    response_type,
    code_challenge,
    code_challenge_method,
    scope,
):
    client = DiagnosticOAuthClient.objects.filter(
        client_id=client_id,
        is_active=True,
    ).first()
    if client is None:
        raise DiagnosticAuthError("Unknown OAuth client.")
    if redirect_uri not in (client.redirect_uris or []):
        raise DiagnosticAuthError(
            "OAuth redirect URI is not registered."
        )
    if response_type != "code":
        raise DiagnosticAuthError(
            "Only response_type=code is supported."
        )
    if not code_challenge or code_challenge_method != "S256":
        raise DiagnosticAuthError("PKCE S256 is required.")

    scopes = set(str(scope or "").split())
    if DIAGNOSTICS_SCOPE not in scopes:
        raise DiagnosticAuthError(
            "diagnostics.read scope is required."
        )
    if not scopes <= SUPPORTED_SCOPES:
        raise DiagnosticAuthError(
            "Unsupported OAuth scope requested."
        )
    return client


def issue_authorization_code(
    *,
    client,
    raw_api_key,
    redirect_uri,
    scope,
    code_challenge,
    resource="",
):
    api_key = _active_api_key_from_raw(
        raw_api_key,
        require_diagnostics=True,
    )
    raw_code = secrets.token_urlsafe(48)
    DiagnosticOAuthAuthorizationCode.objects.create(
        code_hash=token_hash(raw_code),
        client=client,
        api_key=api_key,
        organization=api_key.organization,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        resource=str(resource or "")[:2048],
        expires_at=timezone.now() + AUTH_CODE_TTL,
    )
    return raw_code


def exchange_authorization_code(
    *,
    code,
    client_id,
    redirect_uri,
    code_verifier,
    resource="",
):
    now = timezone.now()
    with transaction.atomic():
        auth_code = (
            DiagnosticOAuthAuthorizationCode.objects.select_for_update()
            .select_related("client", "api_key", "organization")
            .filter(code_hash=token_hash(code))
            .first()
        )
        if auth_code is None:
            raise DiagnosticAuthError(
                "Invalid authorization code."
            )
        if auth_code.used_at is not None:
            raise DiagnosticAuthError(
                "Authorization code has already been used."
            )
        if auth_code.expires_at <= now:
            raise DiagnosticAuthError(
                "Authorization code has expired."
            )
        if auth_code.client.client_id != client_id:
            raise DiagnosticAuthError("OAuth client mismatch.")
        if auth_code.redirect_uri != redirect_uri:
            raise DiagnosticAuthError(
                "OAuth redirect URI mismatch."
            )
        if pkce_s256(code_verifier) != auth_code.code_challenge:
            raise DiagnosticAuthError("PKCE verification failed.")
        if resource and auth_code.resource != resource:
            raise DiagnosticAuthError("OAuth resource mismatch.")
        if (
            not auth_code.api_key.is_active
            or not getattr(
                auth_code.api_key,
                "can_read_diagnostics",
                False,
            )
        ):
            raise DiagnosticAuthError(
                "Diagnostic API key is no longer active."
            )

        raw_access = secrets.token_urlsafe(48)
        raw_refresh = secrets.token_urlsafe(56)
        oauth_token = DiagnosticOAuthToken.objects.create(
            client=auth_code.client,
            api_key=auth_code.api_key,
            organization=auth_code.organization,
            access_token_hash=token_hash(raw_access),
            refresh_token_hash=token_hash(raw_refresh),
            scope=auth_code.scope,
            resource=auth_code.resource,
            expires_at=now + ACCESS_TOKEN_TTL,
            refresh_expires_at=now + REFRESH_TOKEN_TTL,
        )
        auth_code.used_at = now
        auth_code.save(update_fields=["used_at"])

    return oauth_token, raw_access, raw_refresh


def refresh_access_token(
    *,
    refresh_token,
    client_id,
    resource="",
):
    now = timezone.now()
    with transaction.atomic():
        oauth_token = (
            DiagnosticOAuthToken.objects.select_for_update()
            .select_related("client", "organization", "api_key")
            .filter(
                refresh_token_hash=token_hash(refresh_token),
                revoked_at__isnull=True,
            )
            .first()
        )
        if oauth_token is None:
            raise DiagnosticAuthError("Invalid refresh token.")
        if oauth_token.client.client_id != client_id:
            raise DiagnosticAuthError("OAuth client mismatch.")
        if resource and oauth_token.resource != resource:
            raise DiagnosticAuthError("OAuth resource mismatch.")
        if oauth_token.refresh_expires_at <= now:
            raise DiagnosticAuthError(
                "Refresh token has expired."
            )
        if not organization_is_active(oauth_token.organization):
            raise DiagnosticAuthError(
                "Organization account is disabled."
            )
        if (
            not oauth_token.api_key.is_active
            or not getattr(
                oauth_token.api_key,
                "can_read_diagnostics",
                False,
            )
        ):
            raise DiagnosticAuthError(
                "Diagnostic API key is no longer active."
            )

        raw_access = secrets.token_urlsafe(48)
        raw_refresh = secrets.token_urlsafe(56)
        oauth_token.access_token_hash = token_hash(raw_access)
        oauth_token.refresh_token_hash = token_hash(raw_refresh)
        oauth_token.expires_at = now + ACCESS_TOKEN_TTL
        oauth_token.refresh_expires_at = now + REFRESH_TOKEN_TTL
        oauth_token.last_used_at = now
        oauth_token.save(
            update_fields=[
                "access_token_hash",
                "refresh_token_hash",
                "expires_at",
                "refresh_expires_at",
                "last_used_at",
            ]
        )

    return oauth_token, raw_access, raw_refresh
