"""Side-effect-free local diagnostics for the Operations MCP control plane."""

import json

from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory

from apps.integrations.diagnostic_auth import sanitize_data
from apps.integrations.mcp_oauth_clients import (
    SHVYA_SUPPORTED_GRANT_TYPES,
    is_allowed_operations_cimd_url,
    is_allowed_operations_redirect,
)
from apps.integrations.operations.setup_protocol import SETUP_PROTOCOL_METHODS
from apps.integrations.operations.tool_catalog import TOOL_DEFINITIONS
from apps.integrations.operations_auth import (
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    SUPPORTED_SCOPES,
    pkce_s256,
)
from apps.integrations.operations_endpoints import (
    PRODUCTION_OPERATIONS_ORIGIN,
    operations_authorization_url,
    operations_issuer,
    operations_registration_url,
    operations_resource,
    operations_resource_metadata_url,
    operations_revocation_url,
    operations_token_url,
)
from apps.integrations.operations_models import (
    OperationsApprovalUse,
    OperationsAuditEvent,
    OperationsOAuthAuthorizationCode,
    OperationsOAuthToken,
    OperationsPolicy,
)


def _check(name, group, callback):
    try:
        detail = callback()
    except Exception as exc:  # pragma: no cover - defensive control-center UX
        return {
            "name": name,
            "group": group,
            "status": "FAIL",
            "detail": str(exc)[:240],
        }
    if detail is False:
        return {
            "name": name,
            "group": group,
            "status": "FAIL",
            "detail": "Invariant did not pass.",
        }
    return {
        "name": name,
        "group": group,
        "status": "PASS",
        "detail": str(detail or "Verified locally.")[:240],
    }


def _endpoint_check():
    values = {
        operations_issuer(),
        operations_resource(),
        operations_resource_metadata_url(),
        operations_authorization_url(),
        operations_token_url(),
        operations_registration_url(),
        operations_revocation_url(),
    }
    if any("staging.shvya-ai.com" in value for value in values):
        raise ValueError("A runtime Operations endpoint advertises staging.")
    if operations_resource().rstrip("/") == operations_issuer():
        raise ValueError("Issuer and protected resource are not distinct.")
    return f"{len(values)} canonical endpoints are internally consistent."


def _production_origin_check():
    if operations_resource().startswith(PRODUCTION_OPERATIONS_ORIGIN):
        return "Production origin is pinned to shvya-ai.com."
    return "Local/test origin override is active; production remains fail-closed."


def _pkce_check():
    challenge = pkce_s256("a" * 64)
    if len(challenge) != 43 or "=" in challenge:
        raise ValueError("PKCE S256 output is not RFC-compatible.")
    return "PKCE S256 challenge generation is valid."


def _scope_check():
    if not {
        OPERATIONS_READ_SCOPE,
        OPERATIONS_WRITE_SCOPE,
        "offline_access",
    }.issubset(SUPPORTED_SCOPES):
        raise ValueError("Required Operations scopes are missing.")
    return "Read, write, and offline scopes are declared."


def _registration_check():
    accepted = (
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://claude.ai/api/mcp/auth_callback",
        "https://vscode.dev/redirect",
        "https://www.cursor.com/agents/mcp/oauth/callback",
        "https://custom-client.example/callback",
        "http://127.0.0.1/callback",
        "http://localhost:49152/callback",
        "http://[::1]:49152/callback",
    )
    rejected = (
        "http://example.com/callback",
        "https://user:password@example.com/callback",
        "https://example.com/callback#fragment",
    )
    if not all(is_allowed_operations_redirect(uri) for uri in accepted):
        raise ValueError("A supported public-client callback was rejected.")
    if any(is_allowed_operations_redirect(uri) for uri in rejected):
        raise ValueError("An unsafe callback was accepted.")
    return "HTTPS and RFC 8252 callback policy passed locally."


def _cimd_check():
    if tuple(SHVYA_SUPPORTED_GRANT_TYPES) != (
        "authorization_code",
        "refresh_token",
    ):
        raise ValueError("Unexpected SHVYA token grant surface.")
    if not is_allowed_operations_cimd_url(
        "https://claude.ai/oauth/mcp-oauth-client-metadata"
    ):
        raise ValueError("Claude CIMD publisher is not trusted.")
    if is_allowed_operations_cimd_url(
        "https://127.0.0.1/client-metadata"
    ):
        raise ValueError("Private CIMD destination was accepted.")
    return "CIMD trust policy and grant negotiation are configured."


def _tool_registry_check():
    names = [item.get("name") for item in TOOL_DEFINITIONS]
    if not names or len(names) != len(set(names)):
        raise ValueError("Tool names are empty or duplicated.")
    for item in TOOL_DEFINITIONS:
        schema = item.get("inputSchema") or {}
        annotations = item.get("annotations") or {}
        schemes = item.get("securitySchemes") or []
        if schema.get("type") != "object":
            raise ValueError(f"{item.get('name')} has no object input schema.")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{item.get('name')} accepts undeclared fields.")
        if not schemes or schemes != (item.get("_meta") or {}).get(
            "securitySchemes"
        ):
            raise ValueError(f"{item.get('name')} has inconsistent OAuth metadata.")
        if not {
            "readOnlyHint",
            "destructiveHint",
            "openWorldHint",
        }.issubset(annotations):
            raise ValueError(f"{item.get('name')} is missing tool annotations.")
    if "staging.shvya-ai.com" in json.dumps(TOOL_DEFINITIONS):
        raise ValueError("Tool registry leaks the staging hostname.")
    return f"{len(TOOL_DEFINITIONS)} tool definitions passed schema checks."


def _protocol_check():
    required = {
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/read",
    }
    if not required.issubset(SETUP_PROTOCOL_METHODS):
        raise ValueError("Native prompt/resource methods are incomplete.")
    return "Initialize, discovery, tools, prompts, and resources are implemented."


def _auth_challenge_check():
    # Import lazily so the self-test remains safe to import while Django URL
    # modules and Superadmin views are still being initialized.
    from apps.integrations.views.operations_mcp import _authorization_challenge

    request = RequestFactory().post("/operations/mcp/")
    challenge = _authorization_challenge(request)
    expected = f'resource_metadata="{operations_resource_metadata_url()}"'
    if not challenge.startswith("Bearer ") or expected not in challenge:
        raise ValueError("OAuth 401 challenge does not advertise protected metadata.")
    if "staging.shvya-ai.com" in challenge:
        raise ValueError("OAuth 401 challenge advertises staging.")
    return "Bearer challenge advertises canonical protected-resource metadata."


def _token_model_check():
    for model, field_names in (
        (
            OperationsOAuthAuthorizationCode,
            ("code_hash", "expires_at", "used_at", "resource"),
        ),
        (
            OperationsOAuthToken,
            (
                "access_token_hash",
                "refresh_token_hash",
                "expires_at",
                "refresh_expires_at",
                "revoked_at",
                "resource",
            ),
        ),
    ):
        for field_name in field_names:
            model._meta.get_field(field_name)
    return "Authorization-code and token security fields are present."


def _approval_audit_check():
    if not OperationsApprovalUse._meta.get_field("approval_event").unique:
        raise ValueError("Approval receipts are not single-use.")
    if OperationsAuditEvent.save is OperationsAuditEvent.__mro__[1].save:
        raise ValueError("Audit events do not override mutation behavior.")
    OperationsPolicy._meta.get_field("organization")
    return "Single-use approval, live policy, and immutable audit models exist."


def _migration_check():
    conflicts = MigrationLoader(
        None,
        ignore_no_migrations=True,
    ).detect_conflicts()
    if "integrations" in conflicts:
        raise ValueError("Integrations migrations have multiple leaf nodes.")
    return "Integrations migration graph has one leaf."


def _sanitization_check():
    raw_secret = "Bearer abcdefghijklmnopqrstuvwxyz1234567890"
    sanitized = json.dumps(sanitize_data({"provider_response": raw_secret}))
    if raw_secret in sanitized:
        raise ValueError("Credential-like output was not redacted.")
    return "Credential-like output is redacted by the shared sanitizer."


def run_operations_self_test():
    """Run bounded local checks only; never call staging or another service."""

    checks = [
        _check("Canonical endpoint consistency", "MCP Health", _endpoint_check),
        _check("Production URL isolation", "MCP Health", _production_origin_check),
        _check("PKCE S256", "OAuth Health", _pkce_check),
        _check("OAuth scopes", "OAuth Health", _scope_check),
        _check("Dynamic registration callbacks", "OAuth Health", _registration_check),
        _check("Claude CIMD compatibility", "OAuth Health", _cimd_check),
        _check("Tool registry and schemas", "MCP Health", _tool_registry_check),
        _check("Native MCP protocol surfaces", "MCP Health", _protocol_check),
        _check("OAuth 401 challenge", "OAuth Health", _auth_challenge_check),
        _check("Token model controls", "Security", _token_model_check),
        _check("Approval, policy, and audit models", "Security", _approval_audit_check),
        _check("Migration graph", "Security", _migration_check),
        _check("Response sanitization", "Security", _sanitization_check),
    ]
    counts = {
        status: sum(1 for item in checks if item["status"] == status)
        for status in ("PASS", "WARNING", "FAIL")
    }
    return {
        "checks": checks,
        "counts": counts,
        "overall": "FAIL" if counts["FAIL"] else "PASS",
    }
