from __future__ import annotations

import base64
import hashlib
import re
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
    OperationsAuditEvent,
    OperationsOAuthAuthorizationCode,
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsSupportSession,
)
from apps.integrations.operations_policy import (
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    capabilities_for_grant,
    effective_capabilities,
    expand_capabilities,
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

_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")

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
    granted_capabilities: frozenset[str]


def token_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(str(verifier or "").encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _allowed_redirect(uri: str) -> bool:
    try:
        parsed = urlparse(str(uri or ""))
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return False

    # VS Code's remote MCP OAuth flow documents these two redirect URLs.
    # Keep them exact instead of allowing arbitrary localhost/vscode.dev paths.
    if (
        parsed.scheme == "http"
        and host == "127.0.0.1"
        and port == 33418
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
        and port in {None, 443}
        and not parsed.fragment
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
    raw_redirects = [
        str(item or "").strip()
        for item in (redirect_uris or [])
        if str(item or "").strip()
    ]
    redirect_uris = list(dict.fromkeys(raw_redirects))
    if not redirect_uris:
        raise OperationsAuthError("At least one redirect URI is required.")
    if len(redirect_uris) > 8:
        raise OperationsAuthError("At most 8 redirect URIs may be registered.")
    if any(len(uri) > 2048 for uri in redirect_uris):
        raise OperationsAuthError("OAuth redirect URI is too long.")
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
    if (
        code_challenge_method != "S256"
        or not _PKCE_CHALLENGE_RE.fullmatch(str(code_challenge or ""))
    ):
        raise OperationsAuthError(
            "PKCE S256 with a valid 43–128 character challenge is required."
        )

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
    granted_capabilities = sorted(
        capabilities_for_grant(
            role=role,
            organization=organization,
            allow_writes=(
                OPERATIONS_WRITE_SCOPE in requested
            ),
        )
    )

    raw_code = secrets.token_urlsafe(48)
    OperationsOAuthAuthorizationCode.objects.create(
        client=client,
        actor=actor,
        organization=organization,
        role=role,
        code_hash=token_hash(raw_code),
        redirect_uri=redirect_uri,
        scope=normalized_scope,
        granted_capabilities=granted_capabilities,
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
            OperationsOAuthAuthorizationCode.objects.select_for_update(of=("self",))
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
        if not _PKCE_VERIFIER_RE.fullmatch(str(code_verifier or "")):
            raise OperationsAuthError(
                "PKCE verifier must be 43–128 valid unreserved characters."
            )
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
            granted_capabilities=list(
                auth_code.granted_capabilities or []
            ),
            resource=auth_code.resource,
            expires_at=now + ACCESS_TOKEN_TTL,
            refresh_expires_at=now + REFRESH_TOKEN_TTL,
        )
        auth_code.used_at = now
        auth_code.save(update_fields=["used_at"])
    return token, raw_access, raw_refresh


def _record_automatic_grant_revocation(
    *,
    token,
    message,
    support_session,
):
    organization = (
        token.active_organization
        if token.role == ROLE_SUPERADMIN
        else token.organization
    )
    try:
        OperationsAuditEvent.objects.create(
            actor=token.actor,
            role=token.role,
            organization=organization,
            support_session=support_session,
            tool_name="oauth_auto_revoke",
            capability="",
            target_type="oauth_grant",
            target_id=str(token.id),
            reason=str(message or "")[:500],
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint=token_hash(
                "oauth_auto_revoke:"
                + str(token.id)
                + ":"
                + str(message or "")
            ),
            change_summary={
                "access": "revoked",
                "reason_code": "live_authority_invalid",
                "support_session_closed": (
                    support_session is not None
                ),
            },
            duration_ms=0,
            error_code="",
        )
    except Exception:
        # Revocation is a security boundary and must not be undone merely
        # because secondary audit persistence is unavailable.
        return None
    return True


def operations_grant_status(token, *, now=None):
    """Return pure live-authority status for one persisted Operations grant."""

    now = now or timezone.now()
    if token is None:
        return False, "missing_grant", "Operations OAuth grant is missing."
    if token.revoked_at is not None:
        return False, "revoked", "Operations OAuth grant has been revoked."
    if token.refresh_expires_at <= now:
        return False, "grant_expired", "Operations OAuth grant has expired."
    if not token.client.is_active:
        return False, "client_deactivated", "OAuth client has been deactivated."

    actor = token.actor
    if not actor.is_active:
        return False, "user_inactive", "SHVYA user is inactive."

    if token.role == ROLE_SUPERADMIN:
        if not actor.is_superuser:
            return (
                False,
                "superadmin_revoked",
                "Superadmin permission has been revoked.",
            )
        return True, "active", ""

    if token.role == ROLE_ORGANIZATION_ADMIN:
        if (
            actor.is_superuser
            or actor.role != User.Role.ADMIN
            or actor.organization_id != token.organization_id
            or token.organization is None
            or not organization_is_active(token.organization)
            or not policy_for(
                token.organization
            ).organization_admin_enabled
        ):
            return (
                False,
                "organization_authority_revoked",
                "Organization Operations permission has been revoked.",
            )
        return True, "active", ""

    return (
        False,
        "unsupported_role",
        "Unsupported SHVYA Operations role.",
    )


def _deny_and_revoke_live_grant(
    token,
    message,
    *,
    revoke=True,
):
    """Reject a grant whose live SHVYA authority is gone."""

    if revoke:
        organization = (
            token.active_organization
            if token.role == ROLE_SUPERADMIN
            else token.organization
        )
        support_session = (
            OperationsSupportSession.objects.filter(
                token=token,
                organization=organization,
            )
            .order_by("-started_at")
            .first()
            if organization is not None
            else None
        )
        revoked = revoke_token_record(
            token=token
        )
        if revoked is not None:
            _record_automatic_grant_revocation(
                token=token,
                message=message,
                support_session=support_session,
            )
    raise OperationsAuthError(message)


def _validate_live_token(token, *, revoke_on_failure=True):
    valid, _reason_code, message = operations_grant_status(
        token
    )
    if valid:
        return
    _deny_and_revoke_live_grant(
        token,
        message,
        revoke=revoke_on_failure,
    )


def revoke_refresh_grant_if_live_authority_invalid(
    *,
    refresh_token,
):
    """Persist revocation after a failed refresh when live SHVYA authority is gone."""

    token = (
        OperationsOAuthToken.objects.select_related(
            "client",
            "actor",
            "organization",
            "active_organization",
        )
        .filter(
            refresh_token_hash=token_hash(refresh_token),
            revoked_at__isnull=True,
        )
        .first()
    )
    if token is None:
        return False

    try:
        _validate_live_token(token)
    except OperationsAuthError:
        token.refresh_from_db(fields=["revoked_at"])
        return token.revoked_at is not None
    return False


def refresh_access_token(*, refresh_token, client_id, resource=""):
    now = timezone.now()
    with transaction.atomic():
        token = (
            OperationsOAuthToken.objects.select_for_update(of=("self",))
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

        validation_error = None
        try:
            _validate_live_token(
                token,
                revoke_on_failure=False,
            )
        except OperationsAuthError as exc:
            validation_error = exc
            _revoke_locked_token(
                token,
                now=now,
            )

        if validation_error is None:
            raw_access = secrets.token_urlsafe(48)
            raw_refresh = secrets.token_urlsafe(56)
            token.access_token_hash = token_hash(raw_access)
            token.refresh_token_hash = token_hash(raw_refresh)
            token.expires_at = now + ACCESS_TOKEN_TTL
            # Keep the original grant's refresh expiry fixed. Rotation prevents
            # token replay; it must not silently turn a 14-day external-AI grant
            # into an indefinitely renewable credential.
            token.last_used_at = now
            token.save(
                update_fields=[
                    "access_token_hash",
                    "refresh_token_hash",
                    "expires_at",
                    "last_used_at",
                    "updated_at",
                ]
            )

    if validation_error is not None:
        raise validation_error
    return token, raw_access, raw_refresh


def _revoke_locked_token(token, *, now=None):
    now = now or timezone.now()
    if token.revoked_at is not None:
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
    return token


def revoke_token_record(*, token):
    """Revoke a known Operations OAuth row without requiring raw token material."""

    with transaction.atomic():
        locked = (
            OperationsOAuthToken.objects.select_for_update()
            .filter(pk=token.pk)
            .first()
        )
        if locked is None:
            return None
        return _revoke_locked_token(locked)


def end_support_context_record(*, token, organization):
    """End one Superadmin support context without requiring raw OAuth material."""

    now = timezone.now()
    with transaction.atomic():
        locked = (
            OperationsOAuthToken.objects.select_for_update()
            .filter(pk=token.pk, role=ROLE_SUPERADMIN)
            .first()
        )
        if locked is None:
            return 0

        sessions = OperationsSupportSession.objects.filter(
            token=locked,
            organization=organization,
            ended_at__isnull=True,
        )
        ended_count = sessions.update(
            ended_at=now,
            last_seen_at=now,
        )
        if locked.active_organization_id == organization.id:
            locked.active_organization = None
            locked.save(
                update_fields=[
                    "active_organization",
                    "updated_at",
                ]
            )
        return ended_count


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
    token = (
        OperationsOAuthToken.objects.filter(
            revoked_at__isnull=True,
        )
        .filter(
            models.Q(access_token_hash=hashed)
            | models.Q(refresh_token_hash=hashed)
        )
        .first()
    )
    if token is None:
        return None

    return revoke_token_record(
        token=token
    )


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

    granted_capabilities = frozenset(
        expand_capabilities(
            token.granted_capabilities
        )
    )

    OperationsOAuthToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return OperationsIdentity(
        token=token,
        actor=token.actor,
        role=token.role,
        organization=token.organization,
        active_organization=token.active_organization,
        scopes=scopes,
        granted_capabilities=granted_capabilities,
    )
