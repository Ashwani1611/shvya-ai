"""Actor-bound Remote MCP + OAuth endpoints for SHVYA Operations."""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlencode, urlparse

from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.ratelimit import ratelimit
from apps.integrations.diagnostic_auth import (
    request_fingerprint,
    sanitize_data,
    sanitize_text,
)
from apps.integrations.mcp_schema import (
    MCPInputValidationError,
    validate_mcp_arguments,
)
from apps.integrations.models import (
    OperationsAuditEvent,
    OperationsOAuthAuthorizationCode,
    OperationsOAuthClient,
    OperationsSupportSession,
)
from apps.integrations.operations_agent_prompt import OPERATIONS_AGENT_INSTRUCTIONS
from apps.integrations.operations_approval import approval_fingerprint
from apps.integrations.operations_auth import (
    ACCESS_TOKEN_TTL,
    OFFLINE_SCOPE,
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    OperationsAuthError,
    authenticate_bearer,
    available_browser_identities,
    exchange_authorization_code,
    issue_authorization_code,
    refresh_access_token,
    register_client,
    revoke_refresh_grant_if_live_authority_invalid,
    token_hash,
    revoke_token,
    validate_authorization_request,
)
from apps.integrations.operations_endpoints import (
    operations_authorization_url,
    operations_issuer,
    operations_public_url,
    operations_registration_url,
    operations_resource,
    operations_resource_metadata_url,
    operations_revocation_url,
    operations_token_url,
)
from apps.integrations.operations_policy import (
    CAPABILITY_LABELS,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    WRITE_CAPABILITIES,
    approval_required,
    effective_capabilities,
)
from apps.integrations.operations_tools import (
    OperationsApprovalRequired,
    OperationsManualFixRequired,
    OperationsPermissionError,
    OperationsSuperadminRequired,
    OperationsToolError,
    ToolExecution,
    execute_operations_tool,
)
from apps.integrations.operations.setup_catalog import SETUP_TOOL_CAPABILITIES
from apps.integrations.operations.setup_protocol import (
    SETUP_LIBRARY_TOOL_NAMES,
    SETUP_PROTOCOL_METHODS,
    SetupResourceNotFound,
    execute_setup_protocol,
)

from apps.integrations.operations.tool_catalog import (
    DIAGNOSTIC_DEFINITIONS as DIAGNOSTIC_DEFINITIONS,
    KNOWN_TOOLS as KNOWN_TOOLS,
    OAUTH_READ_SCHEMES as OAUTH_READ_SCHEMES,
    OAUTH_WRITE_SCHEMES as OAUTH_WRITE_SCHEMES,
    OWN_TOOL_DEFINITIONS as OWN_TOOL_DEFINITIONS,
    TOOL_CAPABILITIES as TOOL_CAPABILITIES,
    TOOL_DEFINITIONS as TOOL_DEFINITIONS,
    TOOL_INPUT_SCHEMAS as TOOL_INPUT_SCHEMAS,
    _tool as _tool,
    _tool_requires_write_scope as _tool_requires_write_scope,
    _tools_for_identity as _tools_for_identity,
    _write_properties as _write_properties,
)

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = (
    MODERN_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSION,
)
SERVER_INFO = {"name": "shvya-operations", "version": "1.0.0"}
logger = logging.getLogger(__name__)
OAUTH_MAX_BODY_BYTES = 32 * 1024
OAUTH_MAX_STATE_LENGTH = 1024

TOOL_RESPONSE_TEXT_LIMITS = {
    # These tools deliberately expose bounded configuration text after secret
    # redaction. Keep ordinary diagnostics on the sanitizer's 800-char default.
    "get_ai_configuration": 110000,
    "get_organization_configuration": 30000,
    "export_organization_configuration": 250000,
    "get_configuration_dependency_graph": 120000,
    "create_configuration_plan": 120000,
    "import_organization_configuration": 120000,
    "apply_configuration_plan": 120000,
    "get_setup_library_resource": 20000,
    "get_setup_variable_schema": 20000,
    "render_setup_template": 100000,
    "analyze_setup_group_export": 65000,
    "get_setup_intake": 12000,
    "upsert_setup_intake_entry": 12000,
    "archive_setup_intake_entry": 12000,
}

def _oauth_request_too_large(request):
    raw = str(request.META.get("CONTENT_LENGTH") or "").strip()
    if not raw:
        return False
    try:
        return int(raw) > OAUTH_MAX_BODY_BYTES
    except (TypeError, ValueError):
        return True


def _oauth_too_large_response():
    response = JsonResponse(
        {
            "error": "invalid_request",
            "error_description": "OAuth request body is too large.",
        },
        status=413,
    )
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response



def _issuer(request):
    return operations_issuer()


def _resource(request):
    return operations_resource()


def _resource_metadata_url(request):
    return operations_resource_metadata_url()


def _server_meta():
    return {"io.modelcontextprotocol/serverInfo": SERVER_INFO}


def _jsonrpc_result(request_id, result, *, modern=False):
    result = dict(result or {})
    if modern:
        result.setdefault("resultType", "complete")
        meta = dict(result.get("_meta") or {})
        meta.update(_server_meta())
        result["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id, code, message, *, modern=False, data=None):
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        payload["error"]["data"] = data
    if modern:
        payload["error"].setdefault("data", {})
        if isinstance(payload["error"]["data"], dict):
            payload["error"]["data"].setdefault("_meta", _server_meta())
    return payload


def _modern_request(request, payload):
    if request.headers.get("MCP-Protocol-Version") == MODERN_PROTOCOL_VERSION:
        return True
    params = payload.get("params") if isinstance(payload, dict) else {}
    meta = params.get("_meta") if isinstance(params, dict) else {}
    return (
        isinstance(meta, dict)
        and meta.get("io.modelcontextprotocol/protocolVersion")
        == MODERN_PROTOCOL_VERSION
    )


def _validate_modern_headers(request, *, method, params):
    version = request.headers.get("MCP-Protocol-Version")
    routed_method = request.headers.get("Mcp-Method")
    routed_name = request.headers.get("Mcp-Name")
    if version and version != MODERN_PROTOCOL_VERSION:
        raise OperationsToolError("Unsupported MCP protocol version.")
    if routed_method and routed_method != method:
        raise OperationsToolError(
            "Mcp-Method header does not match JSON-RPC method."
        )
    expected_name = (
        str((params or {}).get("name") or "")
        if method in {"tools/call", "prompts/get"} else ""
    )
    if routed_name and expected_name and routed_name != expected_name:
        raise OperationsToolError(
            "Mcp-Name header does not match request parameters."
        )


def _authorization_challenge(
    request,
    *,
    error="invalid_token",
    description="Connect SHVYA Operations to continue.",
):
    safe = sanitize_text(description, limit=200).replace('"', "'")
    return (
        f'Bearer resource_metadata="{_resource_metadata_url(request)}", '
        f'error="{error}", error_description="{safe}"'
    )


def _auth_result(request, request_id, *, modern, description):
    challenge = _authorization_challenge(request, description=description)
    result = {
        "content": [
            {
                "type": "text",
                "text": "Authentication required for SHVYA Operations.",
            }
        ],
        "_meta": {"mcp/www_authenticate": [challenge]},
        "isError": True,
    }
    response = JsonResponse(
        _jsonrpc_result(request_id, result, modern=modern),
        status=401,
    )
    response["WWW-Authenticate"] = challenge
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def operations_oauth_resource_metadata(request):
    return JsonResponse(
        {
            "resource": _resource(request),
            "authorization_servers": [_issuer(request)],
            "scopes_supported": [
                OPERATIONS_READ_SCOPE,
                OPERATIONS_WRITE_SCOPE,
                OFFLINE_SCOPE,
            ],
            "bearer_methods_supported": ["header"],
            "resource_documentation": operations_public_url(
                reverse("crm-connect-hub-shvya-api")
            ),
        }
    )


@require_GET
def operations_oauth_server_metadata(request):
    return JsonResponse(
        {
            "issuer": _issuer(request),
            "authorization_endpoint": operations_authorization_url(),
            "token_endpoint": operations_token_url(),
            "registration_endpoint": operations_registration_url(),
            "revocation_endpoint": operations_revocation_url(),
            "revocation_endpoint_auth_methods_supported": ["none"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "scopes_supported": [
                OPERATIONS_READ_SCOPE,
                OPERATIONS_WRITE_SCOPE,
                OFFLINE_SCOPE,
            ],
            "authorization_response_iss_parameter_supported": True,
        }
    )


@csrf_exempt
@ratelimit(limit=20, window=3600)
@require_POST
def operations_oauth_register(request):
    if _oauth_request_too_large(request):
        return _oauth_too_large_response()
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {
                "error": "invalid_client_metadata",
                "shvya_error_code": "INVALID_CLIENT_METADATA",
            },
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "OAuth client metadata must be a JSON object.",
                "shvya_error_code": "INVALID_CLIENT_METADATA",
            },
            status=400,
        )
    try:
        client = register_client(
            redirect_uris=payload.get("redirect_uris"),
            client_name=payload.get("client_name") or "External AI",
            grant_types=payload.get("grant_types"),
            response_types=payload.get("response_types"),
            application_type=payload.get("application_type") or "web",
            token_endpoint_auth_method=payload.get(
                "token_endpoint_auth_method",
                "none",
            ),
        )
    except OperationsAuthError as exc:
        return JsonResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": sanitize_text(exc, limit=240),
                "shvya_error_code": getattr(
                    exc,
                    "code",
                    "INVALID_CLIENT_METADATA",
                ),
            },
            status=400,
        )
    return JsonResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.client_name,
            "application_type": client.application_type,
            "redirect_uris": client.redirect_uris,
            "grant_types": client.grant_types,
            "response_types": client.response_types,
            "token_endpoint_auth_method": "none",
        },
        status=201,
    )


def _record_oauth_security_event(
    *,
    actor,
    role,
    organization,
    client,
    event,
    scopes,
    capabilities,
    reason,
):
    """Record safe actor-bound OAuth lifecycle metadata without token material."""

    return OperationsAuditEvent.objects.create(
        actor=actor,
        role=role,
        organization=organization,
        support_session=None,
        tool_name=event,
        capability="",
        target_type="oauth_client",
        target_id=str(client.client_id)[:100],
        reason=reason[:500],
        outcome=OperationsAuditEvent.Outcome.SUCCESS,
        request_fingerprint=request_fingerprint(
            {
                "event": event,
                "actor_id": str(actor.id),
                "role": role,
                "client_id": client.client_id,
                "scopes": sorted(set(scopes or [])),
                "capabilities": sorted(
                    set(capabilities or [])
                ),
            }
        ),
        change_summary={
            "client_name": sanitize_text(
                client.client_name or "External AI",
                limit=120,
            ),
            "scopes": sorted(set(scopes or [])),
            "capabilities": sorted(
                set(capabilities or [])
            ),
            "organization_bound": organization is not None,
        },
        duration_ms=0,
        error_code="",
    )


def _authorization_fields(request):
    source = request.POST if request.method == "POST" else request.GET
    return {
        "client_id": source.get("client_id", ""),
        "redirect_uri": source.get("redirect_uri", ""),
        "response_type": source.get("response_type", ""),
        "code_challenge": source.get("code_challenge", ""),
        "code_challenge_method": source.get("code_challenge_method", ""),
        "scope": source.get(
            "scope",
            f"{OPERATIONS_READ_SCOPE} {OPERATIONS_WRITE_SCOPE} {OFFLINE_SCOPE}",
        ),
        "state": str(source.get("state", "")),
        "resource": source.get("resource", _resource(request)),
    }


def _authorization_error_context(exc, *, fallback_stage):
    return {
        "authorization_error": sanitize_text(exc, limit=240),
        "authorization_error_code": str(
            getattr(exc, "code", "OAUTH_REQUEST_INVALID")
        )[:80],
        "authorization_error_stage": str(
            getattr(exc, "stage", fallback_stage)
        )[:80].replace("_", " ").title(),
    }


@ratelimit(limit=30, window=60)
@require_http_methods(["GET", "POST"])
def operations_oauth_authorize(request):
    if request.method == "POST" and _oauth_request_too_large(request):
        return _oauth_too_large_response()
    fields = _authorization_fields(request)
    client = None
    try:
        if len(fields["state"]) > OAUTH_MAX_STATE_LENGTH:
            raise OperationsAuthError(
                "OAuth state is too long."
            )
        client = validate_authorization_request(
            client_id=fields["client_id"],
            redirect_uri=fields["redirect_uri"],
            response_type=fields["response_type"],
            code_challenge=fields["code_challenge"],
            code_challenge_method=fields["code_challenge_method"],
            scope=fields["scope"],
            refresh_remote_metadata=request.method == "GET",
        )
        if fields["resource"] != _resource(request):
            raise OperationsAuthError(
                "OAuth resource does not match the SHVYA Operations MCP endpoint.",
                code="RESOURCE_MISMATCH",
                stage="resource_validation",
            )
    except OperationsAuthError as exc:
        if client is None and fields["client_id"]:
            client = (
                OperationsOAuthClient.objects.only("client_name")
                .filter(client_id=fields["client_id"])
                .first()
            )
        error_context = _authorization_error_context(
            exc,
            fallback_stage="authorization_request",
        )
        return render(
            request,
            "integrations/operations_authorize.html",
            {
                **error_context,
                "fields": fields,
                "client_name": (
                    client.client_name
                    if client is not None
                    else "External AI"
                ),
                "identities": {},
            },
            status=400,
        )

    requested_scopes = sorted(
        set(str(fields.get("scope") or "").split())
    )
    requested_scope_set = set(requested_scopes)

    identities = available_browser_identities(request)
    identity_options = []
    for role, actor in identities.items():
        organization = (
            None if role == ROLE_SUPERADMIN else actor.organization
        )
        policy_capabilities = set(
            effective_capabilities(
                role=role,
                organization=organization,
            )
        )
        capabilities = sorted(
            capability
            for capability in policy_capabilities
            if (
                capability not in WRITE_CAPABILITIES
                or OPERATIONS_WRITE_SCOPE
                in requested_scope_set
            )
        )
        identity_options.append(
            {
                "role": role,
                "actor": actor,
                "capabilities": [
                    {
                        "key": capability,
                        "label": CAPABILITY_LABELS.get(
                            capability,
                            capability,
                        ),
                        "approval_required": (
                            capability in WRITE_CAPABILITIES
                            and approval_required(
                                role=role,
                                organization=organization,
                                capability=capability,
                            )
                        ),
                    }
                    for capability in capabilities
                ],
            }
        )

    current_url = request.get_full_path()
    context = {
        "fields": fields,
        "client_name": client.client_name or "External AI",
        "identities": identities,
        "identity_options": identity_options,
        "requested_scopes": requested_scopes,
        "operations_read_scope": OPERATIONS_READ_SCOPE,
        "operations_write_scope": OPERATIONS_WRITE_SCOPE,
        "role_superadmin": ROLE_SUPERADMIN,
        "role_org_admin": ROLE_ORGANIZATION_ADMIN,
        "superadmin_login_url": (
            reverse("superadmin-login") + "?" + urlencode({"next": current_url})
        ),
        "dashboard_login_url": (
            reverse("crm-login") + "?" + urlencode({"next": current_url})
        ),
        "capability_labels": CAPABILITY_LABELS,
    }
    if request.method == "GET":
        return render(request, "integrations/operations_authorize.html", context)

    role = str(request.POST.get("actor_mode") or "").strip()
    actor = identities.get(role)
    if actor is None:
        actor_error = OperationsAuthError(
            "The selected SHVYA role is not currently authenticated or is not enabled for Operations MCP.",
            code="ACTOR_NOT_AUTHENTICATED",
            stage="consent",
        )
        context.update(
            _authorization_error_context(
                actor_error,
                fallback_stage="consent",
            )
        )
        return render(
            request,
            "integrations/operations_authorize.html",
            context,
            status=403,
        )
    try:
        with transaction.atomic():
            raw_code = issue_authorization_code(
                client=client,
                actor=actor,
                role=role,
                redirect_uri=fields["redirect_uri"],
                scope=fields["scope"],
                code_challenge=fields["code_challenge"],
                resource=fields["resource"],
            )
            authorization_organization = (
                actor.organization
                if role == ROLE_ORGANIZATION_ADMIN
                else None
            )
            issued_code = OperationsOAuthAuthorizationCode.objects.get(
                code_hash=token_hash(raw_code)
            )
            _record_oauth_security_event(
                actor=actor,
                role=role,
                organization=authorization_organization,
                client=client,
                event="oauth_authorize",
                scopes=set(
                    str(issued_code.scope or "").split()
                ),
                capabilities=set(
                    issued_code.granted_capabilities or []
                ),
                reason="External AI Operations OAuth authorization granted.",
            )
    except OperationsAuthError as exc:
        context.update(
            _authorization_error_context(
                exc,
                fallback_stage="authorization_code_issue",
            )
        )
        return render(
            request,
            "integrations/operations_authorize.html",
            context,
            status=403,
        )

    params = {"code": raw_code, "iss": _issuer(request)}
    if fields["state"]:
        params["state"] = fields["state"]
    separator = "&" if urlparse(fields["redirect_uri"]).query else "?"
    return redirect(
        fields["redirect_uri"] + separator + urlencode(params)
    )


@csrf_exempt
@ratelimit(limit=60, window=60)
@require_POST
def operations_oauth_token(request):
    if _oauth_request_too_large(request):
        return _oauth_too_large_response()
    grant_type = request.POST.get("grant_type", "")
    client_id = request.POST.get("client_id", "")
    requested_resource = request.POST.get("resource", "") or _resource(request)
    try:
        with transaction.atomic():
            if grant_type == "authorization_code":
                token, raw_access, raw_refresh = exchange_authorization_code(
                    code=request.POST.get("code", ""),
                    client_id=client_id,
                    redirect_uri=request.POST.get("redirect_uri", ""),
                    code_verifier=request.POST.get("code_verifier", ""),
                    resource=requested_resource,
                )
            elif grant_type == "refresh_token":
                token, raw_access, raw_refresh = refresh_access_token(
                    refresh_token=request.POST.get("refresh_token", ""),
                    client_id=client_id,
                    resource=requested_resource,
                )
            else:
                response = JsonResponse(
                    {
                        "error": "unsupported_grant_type",
                        "error_description": "Use authorization_code or refresh_token.",
                        "shvya_error_code": "AUTHORIZATION_CODE_UNSUPPORTED",
                    },
                    status=400,
                )
                response["Cache-Control"] = "no-store"
                response["Pragma"] = "no-cache"
                return response

            token_event = (
                "oauth_token_refresh"
                if grant_type == "refresh_token"
                else "oauth_token_issue"
            )
            _record_oauth_security_event(
                actor=token.actor,
                role=token.role,
                organization=token.organization,
                client=token.client,
                event=token_event,
                scopes=set(str(token.scope or "").split()),
                capabilities=set(
                    token.granted_capabilities or []
                ),
                reason=(
                    "External AI Operations OAuth token rotated."
                    if grant_type == "refresh_token"
                    else "External AI Operations OAuth token issued."
                ),
            )
    except OperationsAuthError as exc:
        if grant_type == "refresh_token":
            revoke_refresh_grant_if_live_authority_invalid(
                refresh_token=request.POST.get("refresh_token", ""),
            )
        response = JsonResponse(
            {
                "error": "invalid_grant",
                "error_description": sanitize_text(exc, limit=240),
                "shvya_error_code": (
                    "TOKEN_EXCHANGE_FAILED"
                    if getattr(exc, "code", "") == "OAUTH_REQUEST_INVALID"
                    else getattr(exc, "code", "TOKEN_EXCHANGE_FAILED")
                ),
            },
            status=400,
        )
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response

    response = JsonResponse(
        {
            "access_token": raw_access,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "refresh_token": raw_refresh,
            "scope": token.scope,
            "granted_capabilities": sorted(
                set(token.granted_capabilities or [])
            ),
            "resource": token.resource,
        }
    )
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


@csrf_exempt
@ratelimit(limit=60, window=60)
@require_POST
def operations_oauth_revoke(request):
    """RFC 7009-style revocation for actor-bound Operations OAuth grants."""

    if _oauth_request_too_large(request):
        return _oauth_too_large_response()
    raw_token = str(request.POST.get("token") or "").strip()
    if not raw_token:
        response = JsonResponse(
            {
                "error": "invalid_request",
                "error_description": "token is required.",
            },
            status=400,
        )
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response

    try:
        revoked_token = revoke_token(raw_token=raw_token)
    except OperationsAuthError as exc:
        response = JsonResponse(
            {
                "error": "invalid_request",
                "error_description": sanitize_text(exc, limit=200),
            },
            status=400,
        )
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response

    if revoked_token is not None:
        organization = (
            revoked_token.active_organization
            if revoked_token.role == ROLE_SUPERADMIN
            else revoked_token.organization
        )
        support_session = None
        if organization is not None:
            support_session = (
                OperationsSupportSession.objects.filter(
                    token=revoked_token,
                    organization=organization,
                )
                .order_by("-started_at")
                .first()
            )
        OperationsAuditEvent.objects.create(
            actor=revoked_token.actor,
            role=revoked_token.role,
            organization=organization,
            support_session=support_session,
            tool_name="oauth_revoke",
            capability="",
            target_type="oauth_grant",
            target_id=str(revoked_token.id),
            reason="Operations OAuth grant revoked.",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint=request_fingerprint(
                {
                    "event": "oauth_revoke",
                    "token_id": str(revoked_token.id),
                }
            ),
            change_summary={
                "access": "revoked",
                "support_session_closed": support_session is not None,
            },
            duration_ms=0,
            error_code="",
        )

    # Unknown/already-revoked values intentionally return success so the
    # endpoint cannot be used to probe whether a bearer/refresh token exists.
    response = HttpResponse(status=200)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def _record_audit(
    *,
    identity,
    tool_name,
    arguments,
    execution=None,
    outcome=None,
    duration_ms=0,
    error_code="",
    error_reason="",
):
    organization = (
        identity.token.active_organization
        if identity.role == ROLE_SUPERADMIN
        else identity.organization
    )
    support_session = None

    if (
        execution is not None
        and execution.target_type == "platform"
    ):
        organization = None

    if identity.role == ROLE_SUPERADMIN:
        if (
            execution is not None
            and execution.target_type == "platform"
        ):
            support_session = None
        elif organization is not None:
            support_session = (
                OperationsSupportSession.objects.filter(
                    token=identity.token,
                    organization=organization,
                )
                .order_by("-started_at")
                .first()
            )
        elif (
            execution is not None
            and execution.target_type == "organization"
            and execution.target_id
        ):
            # clear_organization_context intentionally removes token context
            # before audit persistence. Resolve only through this token's own
            # support-session history, never through an arbitrary user-supplied
            # organization ID.
            support_session = (
                OperationsSupportSession.objects.select_related("organization")
                .filter(
                    token=identity.token,
                    organization_id=execution.target_id,
                )
                .order_by("-started_at")
                .first()
            )
            if support_session is not None:
                organization = support_session.organization

        if support_session is not None:
            OperationsSupportSession.objects.filter(pk=support_session.pk).update(
                last_seen_at=timezone.now()
            )

    raw_audit_summary = (
        (execution.audit_summary if execution else {})
        or {}
    )
    previous_support_session_id = str(
        raw_audit_summary.get(
            "_previous_support_session_id"
        )
        or ""
    ).strip()
    previous_organization_id = str(
        raw_audit_summary.get(
            "_previous_organization_id"
        )
        or ""
    ).strip()

    if (
        identity.role == ROLE_SUPERADMIN
        and tool_name == "select_organization_context"
        and previous_support_session_id
        and previous_organization_id
    ):
        previous_session = (
            OperationsSupportSession.objects.select_related(
                "organization"
            )
            .filter(
                pk=previous_support_session_id,
                token=identity.token,
                organization_id=previous_organization_id,
            )
            .first()
        )
        if previous_session is not None:
            OperationsAuditEvent.objects.create(
                actor=identity.actor,
                role=identity.role,
                organization=previous_session.organization,
                support_session=previous_session,
                tool_name="support_context_end_on_switch",
                capability=CAP_ORGANIZATION_READ,
                target_type="organization",
                target_id=str(
                    previous_session.organization_id
                ),
                reason=(
                    "SHVYA Support left this organization context "
                    "to continue work elsewhere."
                ),
                outcome=OperationsAuditEvent.Outcome.SUCCESS,
                request_fingerprint=approval_fingerprint(
                    {
                        "event": "support_context_end_on_switch",
                        "support_session_id": str(
                            previous_session.id
                        ),
                    }
                ),
                change_summary={
                    "support_context": "ended",
                    "cause": "organization_switch",
                },
                duration_ms=0,
                error_code="",
            )

    public_audit_summary = (
        dict(raw_audit_summary)
        if isinstance(raw_audit_summary, dict)
        else {}
    )
    public_audit_summary.pop(
        "_previous_support_session_id",
        None,
    )
    public_audit_summary.pop(
        "_previous_organization_id",
        None,
    )
    safe_summary = sanitize_data(
        public_audit_summary
    )
    approval_event_id = str(
        (arguments or {}).get("approval_event_id") or ""
    ).strip()
    if (
        approval_event_id
        and isinstance(safe_summary, dict)
    ):
        safe_summary["approval_event_id"] = approval_event_id[:64]
    reason = (
        execution.reason
        if execution and execution.reason
        else str((arguments or {}).get("reason") or error_reason or "")[:500]
    )
    return OperationsAuditEvent.objects.create(
        actor=identity.actor,
        role=identity.role,
        organization=organization,
        support_session=support_session,
        tool_name=str(tool_name or "")[:100],
        capability=str(execution.capability if execution else "")[:100],
        target_type=str(execution.target_type if execution else "")[:80],
        target_id=str(execution.target_id if execution else "")[:100],
        reason=sanitize_text(reason, limit=500),
        outcome=outcome or (
            execution.outcome if execution else OperationsAuditEvent.Outcome.ERROR
        ),
        request_fingerprint=approval_fingerprint(arguments),
        change_summary=safe_summary if isinstance(safe_summary, dict) else {},
        duration_ms=max(0, int(duration_ms)),
        error_code=str(error_code or "")[:100],
    )


def _setup_protocol_response(request, *, request_id, method, params, modern):
    """Authenticate and audit native MCP reads without a tool-result wrapper."""
    auth_header = request.headers.get("Authorization", "")
    raw_bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    try:
        identity = authenticate_bearer(raw_bearer)
    except OperationsAuthError as exc:
        challenge = _authorization_challenge(request, description=sanitize_text(exc, limit=160))
        response = JsonResponse(
            _jsonrpc_error(
                request_id, -32001, "Authentication required for SHVYA Operations.",
                modern=modern, data={"_meta": {"mcp/www_authenticate": [challenge]}},
            ),
            status=401,
        )
        response["WWW-Authenticate"] = challenge
        response["Cache-Control"] = "no-store"
        return response

    with transaction.atomic():
        started = time.perf_counter()
        execution = None
        error_code = ""
        error_reason = ""
        audit_outcome = None
        status = 200
        try:
            execution = execute_setup_protocol(identity=identity, method=method, params=params)
            # Static guidance is immutable and reviewed; optional prompt
            # context was validated and secret-screened before interpolation.
            payload = _jsonrpc_result(request_id, execution.data, modern=modern)
        except OperationsToolError as exc:
            error_code = exc.code
            audit_outcome = exc.outcome
            denied = isinstance(exc, OperationsPermissionError)
            missing_resource = isinstance(exc, SetupResourceNotFound)
            error_reason = "Setup protocol access denied." if denied else "Setup protocol parameters invalid."
            status = 403 if denied else 404 if missing_resource else 400
            payload = _jsonrpc_error(
                request_id, -32003 if denied else -32002 if missing_resource else -32602,
                sanitize_text(exc, limit=400), modern=modern,
                data={"code": error_code},
            )
        except Exception:
            logger.exception("Operations MCP setup protocol failed: %s", method)
            error_code = "operations_internal_error"
            audit_outcome = OperationsAuditEvent.Outcome.ERROR
            error_reason = "Operations setup request failed safely."
            status = 500
            payload = _jsonrpc_error(
                request_id, -32603, "Operations setup request failed safely.", modern=modern,
            )

        audit = _record_audit(
            identity=identity,
            tool_name=method,
            # Native parameters may contain untrusted operator context. Only
            # fingerprint it; never let a supplied field become an audit reason.
            arguments={"method": method, "parameter_fingerprint": request_fingerprint(params)},
            execution=execution, outcome=audit_outcome,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_code=error_code, error_reason=error_reason,
        )
        if "result" in payload:
            payload["result"].setdefault("_meta", {})["shvya/audit_event_id"] = str(audit.id)
        else:
            payload["error"].setdefault("data", {}).setdefault("_meta", {})["shvya/audit_event_id"] = str(audit.id)

    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt
@ratelimit(limit=240, window=60)
@require_POST
def operations_mcp(request):
    if len(request.body) > 1024 * 1024:
        return JsonResponse(
            _jsonrpc_error(None, -32600, "Request body is too large."),
            status=413,
        )
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            _jsonrpc_error(None, -32700, "Parse error."),
            status=400,
        )
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return JsonResponse(
            _jsonrpc_error(
                payload.get("id") if isinstance(payload, dict) else None,
                -32600,
                "Invalid Request.",
            ),
            status=400,
        )

    is_notification = "id" not in payload
    request_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    modern = _modern_request(request, payload)
    # JSON-RPC notifications never receive a result or error envelope.  Only
    # lifecycle notification names are expected; notification-shaped tool
    # calls are ignored so no mutation can execute without a request ID and a
    # response/audit receipt.
    if is_notification:
        return HttpResponse(status=202)
    try:
        if modern:
            _validate_modern_headers(request, method=method, params=params)
    except OperationsToolError as exc:
        return JsonResponse(
            _jsonrpc_error(
                request_id,
                -32020,
                sanitize_text(exc, limit=200),
                modern=modern,
            ),
            status=400,
        )

    if method == "server/discover":
        return JsonResponse(
            _jsonrpc_result(
                request_id,
                {
                    "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
                    "instructions": OPERATIONS_AGENT_INSTRUCTIONS,
                    "ttlMs": 300000,
                    "cacheScope": "public",
                },
                modern=True,
            )
        )
    if method == "initialize":
        return JsonResponse(
            _jsonrpc_result(
                request_id,
                {
                    "protocolVersion": LEGACY_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
                    "serverInfo": SERVER_INFO,
                    "instructions": OPERATIONS_AGENT_INSTRUCTIONS,
                },
                modern=False,
            )
        )
    if method == "notifications/initialized":
        return HttpResponse(status=202)
    if method == "ping":
        return JsonResponse(
            _jsonrpc_result(request_id, {}, modern=modern)
        )
    if method in SETUP_PROTOCOL_METHODS:
        return _setup_protocol_response(
            request, request_id=request_id, method=method,
            params=payload.get("params", {}), modern=modern,
        )
    if method == "tools/list":
        auth_header = request.headers.get("Authorization", "")
        raw_bearer = (
            auth_header[7:].strip()
            if auth_header.lower().startswith("bearer ")
            else ""
        )
        identity = None
        if raw_bearer:
            try:
                identity = authenticate_bearer(raw_bearer)
            except OperationsAuthError as exc:
                return _auth_result(
                    request,
                    request_id,
                    modern=modern,
                    description=sanitize_text(exc, limit=160),
                )

        tools = _tools_for_identity(identity) if identity is not None else TOOL_DEFINITIONS
        result = {"tools": tools}
        if identity is not None:
            organization = (
                identity.active_organization
                if identity.role == ROLE_SUPERADMIN
                else identity.organization
            )
            audit = _record_audit(
                identity=identity,
                tool_name="tools/list",
                arguments={},
                execution=ToolExecution(
                    data={},
                    capability="",
                    target_type=(
                        "organization"
                        if organization is not None
                        else "platform"
                    ),
                    target_id=(
                        str(organization.id)
                        if organization is not None
                        else ""
                    ),
                    audit_summary={
                        "tool_count": len(tools),
                    },
                ),
                outcome=OperationsAuditEvent.Outcome.SUCCESS,
            )
            result["_meta"] = {
                "shvya/role": identity.role,
                "shvya/effective_tool_count": len(tools),
                "shvya/audit_event_id": str(audit.id),
            }
        if modern:
            result.update(
                {
                    "ttlMs": 300000,
                    "cacheScope": "private" if identity is not None else "public",
                }
            )
        return JsonResponse(
            _jsonrpc_result(request_id, result, modern=modern)
        )
    if method != "tools/call":
        return JsonResponse(
            _jsonrpc_error(
                request_id,
                -32601,
                "Method not found.",
                modern=modern,
            ),
            status=404,
        )

    tool_name = str(params.get("name") or "")
    raw_arguments = params.get("arguments", {})
    arguments_are_object = isinstance(raw_arguments, dict)
    arguments = raw_arguments if arguments_are_object else {}
    if tool_name not in KNOWN_TOOLS:
        return JsonResponse(
            _jsonrpc_error(
                request_id,
                -32602,
                "Unknown SHVYA Operations tool.",
                modern=modern,
            ),
            status=400,
        )

    auth_header = request.headers.get("Authorization", "")
    raw_bearer = (
        auth_header[7:].strip()
        if auth_header.lower().startswith("bearer ")
        else ""
    )
    if not raw_bearer:
        return _auth_result(
            request,
            request_id,
            modern=modern,
            description="Connect an actor-bound SHVYA Operations OAuth session to continue.",
        )
    try:
        identity = authenticate_bearer(raw_bearer)
    except OperationsAuthError as exc:
        return _auth_result(
            request,
            request_id,
            modern=modern,
            description=sanitize_text(exc, limit=160),
        )

    with transaction.atomic():
        started = time.perf_counter()
        execution = None
        error_code = ""
        audit_outcome = None
        error_reason = ""
        try:
            try:
                if not arguments_are_object:
                    raise MCPInputValidationError(
                        "arguments: must be an object"
                    )
                validate_mcp_arguments(
                    arguments,
                    TOOL_INPUT_SCHEMAS[tool_name],
                )
            except MCPInputValidationError as exc:
                raise OperationsToolError(str(exc)) from exc
            execution = execute_operations_tool(
                name=tool_name,
                identity=identity,
                arguments=arguments,
            )
            if tool_name in SETUP_LIBRARY_TOOL_NAMES:
                # Immutable, allowlisted repository assets contain no tenant
                # data. Generic credential heuristics would corrupt their
                # documented identifiers and example placeholders.
                safe_data = execution.data
            else:
                safe_data = sanitize_data(
                    execution.data,
                    text_limit=TOOL_RESPONSE_TEXT_LIMITS.get(tool_name, 800),
                    list_limit=500 if tool_name == "analyze_setup_group_export" else 100,
                )
                if tool_name in SETUP_TOOL_CAPABILITIES and safe_data != execution.data:
                    safe_data["response_sanitized"] = True
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            safe_data,
                            ensure_ascii=False,
                            default=str,
                        ),
                    }
                ],
                "structuredContent": safe_data,
                "isError": False,
            }
        except OperationsApprovalRequired as exc:
            error_code = exc.code
            audit_outcome = exc.outcome
            error_reason = str(exc)
            result = {
                "content": [{"type": "text", "text": sanitize_text(exc, limit=400)}],
                "structuredContent": {
                    "status": "APPROVAL_REQUIRED",
                    "error": sanitize_text(exc, limit=400),
                },
                "isError": True,
            }
        except OperationsSuperadminRequired as exc:
            error_code = exc.code
            audit_outcome = exc.outcome
            error_reason = str(exc)
            result = {
                "content": [{"type": "text", "text": sanitize_text(exc, limit=400)}],
                "structuredContent": {
                    "status": "SUPERADMIN_REQUIRED",
                    "error": sanitize_text(exc, limit=400),
                },
                "isError": True,
            }
        except OperationsManualFixRequired as exc:
            error_code = exc.code
            audit_outcome = exc.outcome
            error_reason = str(exc)
            result = {
                "content": [{"type": "text", "text": sanitize_text(exc, limit=400)}],
                "structuredContent": {
                    "status": "MANUAL_FIX_REQUIRED",
                    "error": sanitize_text(exc, limit=400),
                },
                "isError": True,
            }
        except OperationsPermissionError as exc:
            error_code = exc.code
            audit_outcome = exc.outcome
            error_reason = str(exc)
            result = {
                "content": [{"type": "text", "text": sanitize_text(exc, limit=400)}],
                "structuredContent": {
                    "status": "NOT_ALLOWED",
                    "error": sanitize_text(exc, limit=400),
                },
                "isError": True,
            }
        except OperationsToolError as exc:
            error_code = exc.code
            audit_outcome = exc.outcome
            error_reason = str(exc)
            result = {
                "content": [{"type": "text", "text": sanitize_text(exc, limit=400)}],
                "structuredContent": {
                    "status": "FAILED",
                    "error": sanitize_text(exc, limit=400),
                },
                "isError": True,
            }
        except Exception:
            logger.exception(
                "Operations MCP tool failed unexpectedly: %s",
                tool_name,
            )
            error_code = "operations_internal_error"
            audit_outcome = OperationsAuditEvent.Outcome.ERROR
            error_reason = "Operations tool failed safely."
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "The Operations request failed safely. No provider secret, "
                            "raw internal error, or cross-tenant data was exposed."
                        ),
                    }
                ],
                "structuredContent": {"status": "FAILED", "error": "operations_request_failed"},
                "isError": True,
            }

        duration_ms = max(
            0,
            int((time.perf_counter() - started) * 1000),
        )
        audit = _record_audit(
            identity=identity,
            tool_name=tool_name,
            arguments=arguments,
            execution=execution,
            outcome=audit_outcome,
            duration_ms=duration_ms,
            error_code=error_code,
            error_reason=error_reason,
        )
        result.setdefault("_meta", {})
        result["_meta"]["shvya/audit_event_id"] = str(audit.id)
        if (
            execution is not None
            and execution.outcome == OperationsAuditEvent.Outcome.DRY_RUN
            and isinstance(result.get("structuredContent"), dict)
            and result["structuredContent"].get("approval_required") is True
        ):
            result["structuredContent"]["approval_event_id"] = str(audit.id)
            result["structuredContent"]["approval_expires_in_seconds"] = 1800
            # Keep MCP text and structured payloads semantically identical so
            # clients that primarily consume text still receive the approval
            # receipt required for the execution turn.
            if result.get("content") and isinstance(result["content"][0], dict):
                result["content"][0]["text"] = json.dumps(
                    result["structuredContent"],
                    ensure_ascii=False,
                    default=str,
                )

    response = JsonResponse(
        _jsonrpc_result(request_id, result, modern=modern)
    )
    response["Cache-Control"] = "no-store"
    return response
