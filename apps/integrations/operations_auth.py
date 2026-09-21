from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.db import models, transaction
from django.utils import timezone

from apps.accounts.session_utils import get_session_store
from apps.organizations.access import crm_user_is_authorized, organization_is_active
from apps.integrations.operations_models import (
    OperationsOAuthAuthorizationCode,
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsSupportSession,
)
from apps.integrations.operations_policy import (
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    effective_capabilities,
    policy_for,
)

OPERATIONS_READ_SCOPE = "operations.read"
OPERATIONS_WRITE_SCOPE = "operations.write"
OFFLINE_SCOPE = "offline_access"
SUPPORTED_SCOPES = {
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    OFFLINE_SCOPE,
}
ACCESS_TOKEN_TTL = timedelta(hours=4)
REFRESH_TOKEN_TTL = timedelta(days=14)
AUTH_CODE_TTL = timedelta(minutes=5)

User = get_user_model()


class OperationsAuthError(ValueError):
    pass


@dataclass(frozen=True)
class OperationsIdentity:
    token: OperationsOAuthToken
    actor: object
    role: str
    organization: object | None
    active_organization: object | None
    scopes: frozenset[str]


def token_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(str(verifier or "").encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _allowed_redirect(uri: str) -> bool:
    parsed = urlparse(str(uri or ""))
    host = (parsed.hostname or "").lower()

    # VS Code's remote MCP OAuth flow documents these two redirect URLs.
    # Keep them exact instead of allowing arbitrary localhost/vscode.dev paths.
    if (
        parsed.scheme == "http"
        and host == "127.0.0.1"
        and parsed.port == 33418
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    ):
        return True
    if (
        parsed.scheme == "https"
        and host == "vscode.dev"
        and parsed.path == "/redirect"
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    ):
        return True

    allowed = (
        host == "chatgpt.com"
        or host.endswith(".chatgpt.com")
        or host == "openai.com"
        or host.endswith(".openai.com")
        or host == "claude.ai"
        or host.endswith(".claude.ai")
        or host == "anthropic.com"
        or host.endswith(".anthropic.com")
    )
    return (
        parsed.scheme == "https"
        and allowed
        and parsed.username is None
        and parsed.password is None
    )


def register_client(
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
        raise OperationsAuthError("At least one redirect URI is required.")
    if any(not _allowed_redirect(uri) for uri in redirect_uris):
        raise OperationsAuthError(
            "Operations MCP accepts only approved ChatGPT/OpenAI/Claude/Anthropic or exact VS Code MCP redirect URIs."
        )

    grant_types = list(grant_types or ["authorization_code", "refresh_token"])
    response_types = list(response_types or ["code"])
    if not set(grant_types) <= {"authorization_code", "refresh_token"}:
        raise OperationsAuthError("Unsupported OAuth grant type.")
    if "authorization_code" not in grant_types:
        raise OperationsAuthError("authorization_code grant is required.")
    if set(response_types) != {"code"}:
        raise OperationsAuthError("Only response_type=code is supported.")
    if str(application_type or "web") not in {"web", "native"}:
        raise OperationsAuthError("Unsupported OAuth application_type.")

    return OperationsOAuthClient.objects.create(
        client_id="shvya_ops_" + secrets.token_urlsafe(24),
        client_name=str(client_name or "External AI").strip()[:200],
        application_type=str(application_type or "web"),
        redirect_uris=redirect_uris,
        grant_types=grant_types,
        response_types=response_types,
    )


def validate_authorization_request(
    *,
    client_id,
    redirect_uri,
    response_type,
    code_challenge,
    code_challenge_method,
    scope,
):
    client = OperationsOAuthClient.objects.filter(
        client_id=client_id,
        is_active=True,
    ).first()
    if client is None:
        raise OperationsAuthError("Unknown OAuth client.")
    if redirect_uri not in (client.redirect_uris or []):
        raise OperationsAuthError("OAuth redirect URI is not registered.")
    if response_type != "code":
        raise OperationsAuthError("Only response_type=code is supported.")
    if not code_challenge or code_challenge_method != "S256":
        raise OperationsAuthError("PKCE S256 is required.")

    scopes = set(str(scope or "").split())
    if OPERATIONS_READ_SCOPE not in scopes:
        raise OperationsAuthError("operations.read scope is required.")
    if not scopes <= SUPPORTED_SCOPES:
        raise OperationsAuthError("Unsupported OAuth scope requested.")
    return client


def _session_user(request, area: str):
    session = get_session_store(request, area)
    user_id = session.get(SESSION_KEY)
    backend = session.get(BACKEND_SESSION_KEY)
    session_hash = session.get(HASH_SESSION_KEY)
    if not user_id or not backend or not session_hash:
        return None
    try:
        user = User.objects.select_related("organization").get(pk=user_id)
    except User.DoesNotExist:
        return None
    if session_hash != user.get_session_auth_hash() or not user.is_active:
        return None
    if area == "superadmin":
        return user if user.is_superuser else None
    if area == "dashboard":
        return user if crm_user_is_authorized(user) else None
    return None


def available_browser_identities(request):
    result = {}
    superadmin = _session_user(request, "superadmin")
    if superadmin is not None:
        result[ROLE_SUPERADMIN] = superadmin

    org_user = _session_user(request, "dashboard")
    if (
        org_user is not None
        and getattr(org_user, "role", None) == User.Role.ADMIN
        and getattr(org_user, "organization_id", None)
        and organization_is_active(org_user.organization)
    ):
        policy = policy_for(org_user.organization)
        if policy.organization_admin_enabled:
            result[ROLE_ORGANIZATION_ADMIN] = org_user
    return result


def issue_authorization_code(
    *,
    client,
    actor,
    role,
    redirect_uri,
    scope,
    code_challenge,
    resource,
):
    if role == ROLE_SUPERADMIN:
        if not actor.is_active or not actor.is_superuser:
            raise OperationsAuthError("Superadmin authorization is no longer valid.")
        organization = None
    elif role == ROLE_ORGANIZATION_ADMIN:
        if (
            not actor.is_active
            or actor.is_superuser
            or actor.role != User.Role.ADMIN
            or actor.organization_id is None
            or not organization_is_active(actor.organization)
        ):
            raise OperationsAuthError("Organization admin authorization is no longer valid.")
        policy = policy_for(actor.organization)
        if not policy.organization_admin_enabled:
            raise OperationsAuthError(
                "External AI Operations access is disabled by SHVYA Superadmin."
            )
        organization = actor.organization
    else:
        raise OperationsAuthError("Unsupported SHVYA Operations role.")

    requested = set(str(scope or "").split())
    if OPERATIONS_WRITE_SCOPE in requested:
        capabilities = effective_capabilities(role=role, organization=organization)
        if role != ROLE_SUPERADMIN and not any(item.endswith(".write") for item in capabilities):
            requested.discard(OPERATIONS_WRITE_SCOPE)
    normalized_scope = " ".join(sorted(requested))

    raw_code = secrets.token_urlsafe(48)
    OperationsOAuthAuthorizationCode.objects.create(
        client=client,
        actor=actor,
        organization=organization,
        role=role,
        code_hash=token_hash(raw_code),
        redirect_uri=redirect_uri,
        scope=normalized_scope,
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
            OperationsOAuthAuthorizationCode.objects.select_for_update()
            .select_related("client", "actor", "organization")
            .filter(code_hash=token_hash(code))
            .first()
        )
        if auth_code is None:
            raise OperationsAuthError("Invalid authorization code.")
        if auth_code.used_at is not None:
            raise OperationsAuthError("Authorization code has already been used.")
        if auth_code.expires_at <= now:
            raise OperationsAuthError("Authorization code has expired.")
        if auth_code.client.client_id != client_id:
            raise OperationsAuthError("OAuth client mismatch.")
        if auth_code.redirect_uri != redirect_uri:
            raise OperationsAuthError("OAuth redirect URI mismatch.")
        if pkce_s256(code_verifier) != auth_code.code_challenge:
            raise OperationsAuthError("PKCE verification failed.")
        if resource and auth_code.resource != resource:
            raise OperationsAuthError("OAuth resource mismatch.")

        actor = auth_code.actor
        if not actor.is_active:
            raise OperationsAuthError("SHVYA user is inactive.")
        if auth_code.role == ROLE_SUPERADMIN:
            if not actor.is_superuser:
                raise OperationsAuthError("Superadmin permission has been revoked.")
        elif auth_code.role == ROLE_ORGANIZATION_ADMIN:
            if (
                actor.is_superuser
                or actor.organization_id != auth_code.organization_id
                or actor.role != User.Role.ADMIN
                or not organization_is_active(auth_code.organization)
                or not policy_for(auth_code.organization).organization_admin_enabled
            ):
                raise OperationsAuthError("Organization Operations permission has been revoked.")
        else:
            raise OperationsAuthError("Unsupported SHVYA Operations role.")

        raw_access = secrets.token_urlsafe(48)
        raw_refresh = secrets.token_urlsafe(56)
        token = OperationsOAuthToken.objects.create(
            client=auth_code.client,
            actor=actor,
            organization=auth_code.organization,
            role=auth_code.role,
            access_token_hash=token_hash(raw_access),
            refresh_token_hash=token_hash(raw_refresh),
            scope=auth_code.scope,
            resource=auth_code.resource,
            expires_at=now + ACCESS_TOKEN_TTL,
            refresh_expires_at=now + REFRESH_TOKEN_TTL,
        )
        auth_code.used_at = now
        auth_code.save(update_fields=["used_at"])
    return token, raw_access, raw_refresh


def _validate_live_token(token):
    if not token.client.is_active:
        raise OperationsAuthError("OAuth client has been deactivated.")
    actor = token.actor
    if not actor.is_active:
        raise OperationsAuthError("SHVYA user is inactive.")

    if token.role == ROLE_SUPERADMIN:
        if not actor.is_superuser:
            raise OperationsAuthError("Superadmin permission has been revoked.")
        return

    if token.role == ROLE_ORGANIZATION_ADMIN:
        if (
            actor.is_superuser
            or actor.role != User.Role.ADMIN
            or actor.organization_id != token.organization_id
            or token.organization is None
            or not organization_is_active(token.organization)
            or not policy_for(token.organization).organization_admin_enabled
        ):
            raise OperationsAuthError("Organization Operations permission has been revoked.")
        return

    raise OperationsAuthError("Unsupported SHVYA Operations role.")


def refresh_access_token(*, refresh_token, client_id, resource=""):
    now = timezone.now()
    with transaction.atomic():
        token = (
            OperationsOAuthToken.objects.select_for_update()
            .select_related("client", "actor", "organization", "active_organization")
            .filter(
                refresh_token_hash=token_hash(refresh_token),
                revoked_at__isnull=True,
            )
            .first()
        )
        if token is None:
            raise OperationsAuthError("Invalid refresh token.")
        if token.client.client_id != client_id:
            raise OperationsAuthError("OAuth client mismatch.")
        if resource and token.resource != resource:
            raise OperationsAuthError("OAuth resource mismatch.")
        if token.refresh_expires_at <= now:
            raise OperationsAuthError("Refresh token has expired.")
        _validate_live_token(token)

        raw_access = secrets.token_urlsafe(48)
        raw_refresh = secrets.token_urlsafe(56)
        token.access_token_hash = token_hash(raw_access)
        token.refresh_token_hash = token_hash(raw_refresh)
        token.expires_at = now + ACCESS_TOKEN_TTL
        token.refresh_expires_at = now + REFRESH_TOKEN_TTL
        token.last_used_at = now
        token.save(
            update_fields=[
                "access_token_hash",
                "refresh_token_hash",
                "expires_at",
                "refresh_expires_at",
                "last_used_at",
                "updated_at",
            ]
        )
    return token, raw_access, raw_refresh


def revoke_token(*, raw_token: str):
    """Revoke an Operations OAuth grant by either access or refresh token.

    Returns the revoked token row when found. Unknown tokens deliberately
    behave like successful revocation at the HTTP boundary to avoid token
    enumeration.
    """

    raw_token = str(raw_token or "").strip()
    if not raw_token:
        raise OperationsAuthError("Missing token.")

    hashed = token_hash(raw_token)
    now = timezone.now()
    with transaction.atomic():
        token = (
            OperationsOAuthToken.objects.select_for_update()
            .filter(revoked_at__isnull=True)
            .filter(
                models.Q(access_token_hash=hashed)
                | models.Q(refresh_token_hash=hashed)
            )
            .first()
        )
        if token is None:
            return None

        token.revoked_at = now
        token.save(update_fields=["revoked_at", "updated_at"])
        OperationsSupportSession.objects.filter(
            token=token,
            ended_at__isnull=True,
        ).update(
            ended_at=now,
            last_seen_at=now,
        )
    return True



def authenticate_bearer(raw_bearer: str) -> OperationsIdentity:
    raw_bearer = str(raw_bearer or "").strip()
    if not raw_bearer:
        raise OperationsAuthError("Missing bearer token.")

    now = timezone.now()
    token = (
        OperationsOAuthToken.objects.select_related(
            "client",
            "actor",
            "organization",
            "active_organization",
        )
        .filter(
            access_token_hash=token_hash(raw_bearer),
            revoked_at__isnull=True,
        )
        .first()
    )
    if token is None:
        raise OperationsAuthError("Invalid Operations MCP bearer token.")
    if token.expires_at <= now:
        raise OperationsAuthError("OAuth access token has expired.")
    _validate_live_token(token)

    scopes = frozenset(str(token.scope or "").split())
    if OPERATIONS_READ_SCOPE not in scopes:
        raise OperationsAuthError("OAuth token is missing operations.read scope.")

    OperationsOAuthToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return OperationsIdentity(
        token=token,
        actor=token.actor,
        role=token.role,
        organization=token.organization,
        active_organization=token.active_organization,
        scopes=scopes,
    )
