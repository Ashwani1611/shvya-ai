"""Remote, read-only MCP + OAuth endpoints for SHVYA diagnostics."""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode, urlparse

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
)

from apps.core.ratelimit import ratelimit
from apps.integrations.diagnostic_auth import (
    ACCESS_TOKEN_TTL,
    DIAGNOSTICS_SCOPE,
    OFFLINE_SCOPE,
    DiagnosticAuthError,
    authenticate_bearer,
    exchange_authorization_code,
    issue_authorization_code,
    refresh_access_token,
    register_oauth_client,
    request_fingerprint,
    sanitize_data,
    sanitize_text,
    validate_authorization_request,
)
from apps.integrations.diagnostic_tools import (
    DiagnosticToolError,
    execute_tool,
)
from apps.integrations.models import DiagnosticAccessLog

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"

SERVER_INFO = {
    "name": "shvya-diagnostics",
    "version": "1.0.0",
}

OAUTH_SCHEMES = [
    {
        "type": "oauth2",
        "scopes": [
            DIAGNOSTICS_SCOPE,
            OFFLINE_SCOPE,
        ],
    }
]


TOOL_DEFINITIONS = [
    {
        "name": "get_workspace_profile",
        "title": "Get SHVYA workspace",
        "description": (
            "Identify the SHVYA organization connected to this diagnostic session. "
            "Returns only workspace identity, never users or credentials."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "minLength": 1,
                },
                "name": {"type": "string"},
                "nickname": {"type": "string"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
        "securitySchemes": OAUTH_SCHEMES,
        "_meta": {"openai/profile": True},
    },
    {
        "name": "find_leads",
        "title": "Find SHVYA leads",
        "description": (
            "Find leads in the authorized organization by lead UUID, name, phone, "
            "or email. Use this first when the user gives a person, number, or email "
            "instead of a lead ID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_lead_snapshot",
        "title": "Inspect a SHVYA lead",
        "description": (
            "Return a compact CRM snapshot for one lead, including pipeline, stage, "
            "safe attributes, source, AI switches, and activity counts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {
                    "type": "string",
                    "format": "uuid",
                }
            },
            "required": ["lead_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_conversation",
        "title": "Read lead conversation",
        "description": (
            "Read recent WhatsApp and/or Instagram messages for one lead in "
            "chronological order. Media URLs, access tokens, provider credentials, "
            "and raw webhook payloads are not returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "channel": {
                    "type": "string",
                    "enum": [
                        "all",
                        "whatsapp",
                        "instagram",
                    ],
                    "default": "all",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 30,
                },
            },
            "required": ["lead_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "trace_message",
        "title": "Trace a message",
        "description": (
            "Trace one WhatsApp or Instagram message by SHVYA message UUID or "
            "provider message ID. Shows safe processing, AI, hosted-job, and workflow "
            "status without raw webhook payloads or provider secrets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "minLength": 1,
                }
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_ai_diagnostics",
        "title": "Diagnose SHVYA AI",
        "description": (
            "Run SHVYA's read-only AI engagement diagnostics for one lead: "
            "permissions, credits, qualification state, execution markers, "
            "connection eligibility, and safe blocker codes. Does not call an AI "
            "provider or modify CRM data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {
                    "type": "string",
                    "format": "uuid",
                }
            },
            "required": ["lead_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_integration_health",
        "title": "Check channel integrations",
        "description": (
            "Check the authorized organization's WhatsApp and Instagram connection "
            "health. Only presence/status flags are returned for credentials; "
            "credential values are never exposed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_workflow_trace",
        "title": "Trace SHVYA workflows",
        "description": (
            "Inspect recent workflow/trigger events and runs for one lead. Returns "
            "rule names, statuses, timestamps, and sanitized details without internal "
            "action configuration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                },
            },
            "required": ["lead_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recent_errors",
        "title": "Get recent SHVYA errors",
        "description": (
            "Return sanitized recent failures for WhatsApp, Instagram, hosted "
            "automation, webhooks, and workflows in the authorized organization."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 168,
                    "default": 24,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_runtime_health",
        "title": "Check SHVYA runtime health",
        "description": (
            "Return compact organization-scoped health counters for lead capture, "
            "WhatsApp, Instagram, AI/hosted jobs, and workflows. No secrets or "
            "cross-tenant totals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

for _tool in TOOL_DEFINITIONS:
    _tool.setdefault(
        "annotations",
        {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    )
    _tool.setdefault(
        "securitySchemes",
        OAUTH_SCHEMES,
    )


def _issuer(request):
    return request.build_absolute_uri("/").rstrip("/")


def _resource(request):
    return request.build_absolute_uri(
        reverse("shvya-diagnostic-mcp")
    )


def _resource_metadata_url(request):
    return request.build_absolute_uri(
        reverse(
            "shvya-diagnostic-oauth-resource-metadata"
        )
    )


def _authorization_challenge(
    request,
    *,
    error="invalid_token",
    description=(
        "Connect SHVYA diagnostics to continue."
    ),
):
    safe_description = sanitize_text(
        description,
        limit=200,
    ).replace('"', "'")
    return (
        f'Bearer resource_metadata="{_resource_metadata_url(request)}", '
        f'error="{error}", '
        f'error_description="{safe_description}"'
    )


def _server_meta():
    return {
        "io.modelcontextprotocol/serverInfo": (
            SERVER_INFO
        )
    }


def _jsonrpc_result(
    request_id,
    result,
    *,
    modern=False,
):
    result = dict(result or {})
    if modern:
        result.setdefault(
            "resultType",
            "complete",
        )
        meta = dict(
            result.get("_meta") or {}
        )
        meta.update(_server_meta())
        result["_meta"] = meta
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _jsonrpc_error(
    request_id,
    code,
    message,
    *,
    modern=False,
    data=None,
):
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if data is not None:
        payload["error"]["data"] = data
    if modern:
        payload["error"].setdefault(
            "data",
            {},
        )
        if isinstance(
            payload["error"]["data"],
            dict,
        ):
            payload["error"]["data"].setdefault(
                "_meta",
                _server_meta(),
            )
    return payload


def _modern_request(
    request,
    payload,
):
    if (
        request.headers.get(
            "MCP-Protocol-Version"
        )
        == MODERN_PROTOCOL_VERSION
    ):
        return True

    params = (
        payload.get("params")
        if isinstance(payload, dict)
        else {}
    )
    meta = (
        params.get("_meta")
        if isinstance(params, dict)
        else {}
    )
    return (
        isinstance(meta, dict)
        and meta.get(
            "io.modelcontextprotocol/protocolVersion"
        )
        == MODERN_PROTOCOL_VERSION
    )


def _validate_modern_headers(
    request,
    *,
    method,
    params,
):
    version = request.headers.get(
        "MCP-Protocol-Version"
    )
    routed_method = request.headers.get(
        "Mcp-Method"
    )
    routed_name = request.headers.get(
        "Mcp-Name"
    )

    if (
        version
        and version != MODERN_PROTOCOL_VERSION
    ):
        raise DiagnosticToolError(
            "Unsupported MCP protocol version."
        )
    if (
        routed_method
        and routed_method != method
    ):
        raise DiagnosticToolError(
            "Mcp-Method header does not match JSON-RPC method."
        )

    expected_name = ""
    if method == "tools/call":
        expected_name = str(
            (params or {}).get("name") or ""
        )

    if (
        routed_name
        and expected_name
        and routed_name != expected_name
    ):
        raise DiagnosticToolError(
            "Mcp-Name header does not match request parameters."
        )


def _tool_auth_result(
    request,
    request_id,
    *,
    modern,
    description,
):
    challenge = _authorization_challenge(
        request,
        description=description,
    )
    result = {
        "content": [
            {
                "type": "text",
                "text": (
                    "Authentication required for SHVYA diagnostics."
                ),
            }
        ],
        "_meta": {
            "mcp/www_authenticate": [
                challenge
            ]
        },
        "isError": True,
    }
    response = JsonResponse(
        _jsonrpc_result(
            request_id,
            result,
            modern=modern,
        )
    )
    response["WWW-Authenticate"] = challenge
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def diagnostic_oauth_resource_metadata(
    request,
):
    return JsonResponse(
        {
            "resource": _resource(request),
            "authorization_servers": [
                _issuer(request)
            ],
            "scopes_supported": [
                DIAGNOSTICS_SCOPE,
                OFFLINE_SCOPE,
            ],
            "resource_documentation": (
                request.build_absolute_uri(
                    reverse(
                        "crm-connect-hub-shvya-api"
                    )
                )
            ),
        }
    )


@require_GET
def diagnostic_oauth_server_metadata(
    request,
):
    issuer = _issuer(request)
    return JsonResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": (
                request.build_absolute_uri(
                    reverse(
                        "shvya-diagnostic-oauth-authorize"
                    )
                )
            ),
            "token_endpoint": (
                request.build_absolute_uri(
                    reverse(
                        "shvya-diagnostic-oauth-token"
                    )
                )
            ),
            "registration_endpoint": (
                request.build_absolute_uri(
                    reverse(
                        "shvya-diagnostic-oauth-register"
                    )
                )
            ),
            "response_types_supported": [
                "code"
            ],
            "grant_types_supported": [
                "authorization_code",
                "refresh_token",
            ],
            "code_challenge_methods_supported": [
                "S256"
            ],
            "token_endpoint_auth_methods_supported": [
                "none"
            ],
            "scopes_supported": [
                DIAGNOSTICS_SCOPE,
                OFFLINE_SCOPE,
            ],
            "authorization_response_iss_parameter_supported": True,
        }
    )


@csrf_exempt
@ratelimit(
    limit=20,
    window=3600,
)
@require_POST
def diagnostic_oauth_register(
    request,
):
    try:
        payload = json.loads(
            request.body.decode("utf-8")
            or "{}"
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return JsonResponse(
            {
                "error": (
                    "invalid_client_metadata"
                )
            },
            status=400,
        )

    if (
        payload.get(
            "token_endpoint_auth_method",
            "none",
        )
        != "none"
    ):
        return JsonResponse(
            {
                "error": (
                    "invalid_client_metadata"
                ),
                "error_description": (
                    "SHVYA diagnostics supports public PKCE clients only."
                ),
            },
            status=400,
        )

    try:
        client = register_oauth_client(
            redirect_uris=payload.get(
                "redirect_uris"
            ),
            client_name=(
                payload.get("client_name")
                or "ChatGPT"
            ),
            grant_types=payload.get(
                "grant_types"
            ),
            response_types=payload.get(
                "response_types"
            ),
            application_type=(
                payload.get(
                    "application_type"
                )
                or "web"
            ),
        )
    except DiagnosticAuthError as exc:
        return JsonResponse(
            {
                "error": (
                    "invalid_client_metadata"
                ),
                "error_description": (
                    sanitize_text(
                        exc,
                        limit=240,
                    )
                ),
            },
            status=400,
        )

    return JsonResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(
                client.created_at.timestamp()
            ),
            "client_name": client.client_name,
            "application_type": (
                client.application_type
            ),
            "redirect_uris": (
                client.redirect_uris
            ),
            "grant_types": client.grant_types,
            "response_types": (
                client.response_types
            ),
            "token_endpoint_auth_method": (
                "none"
            ),
        },
        status=201,
    )


def _authorization_fields(request):
    source = (
        request.POST
        if request.method == "POST"
        else request.GET
    )
    return {
        "client_id": source.get(
            "client_id",
            "",
        ),
        "redirect_uri": source.get(
            "redirect_uri",
            "",
        ),
        "response_type": source.get(
            "response_type",
            "",
        ),
        "code_challenge": source.get(
            "code_challenge",
            "",
        ),
        "code_challenge_method": (
            source.get(
                "code_challenge_method",
                "",
            )
        ),
        "scope": source.get(
            "scope",
            f"{DIAGNOSTICS_SCOPE} {OFFLINE_SCOPE}",
        ),
        "state": source.get(
            "state",
            "",
        ),
        "resource": source.get(
            "resource",
            _resource(request),
        ),
    }


@ratelimit(
    limit=30,
    window=60,
)
@require_http_methods(
    ["GET", "POST"]
)
def diagnostic_oauth_authorize(
    request,
):
    fields = _authorization_fields(
        request
    )
    try:
        client = (
            validate_authorization_request(
                client_id=fields[
                    "client_id"
                ],
                redirect_uri=fields[
                    "redirect_uri"
                ],
                response_type=fields[
                    "response_type"
                ],
                code_challenge=fields[
                    "code_challenge"
                ],
                code_challenge_method=(
                    fields[
                        "code_challenge_method"
                    ]
                ),
                scope=fields["scope"],
            )
        )
        if fields["resource"] != _resource(
            request
        ):
            raise DiagnosticAuthError(
                "OAuth resource does not match the SHVYA diagnostic MCP endpoint."
            )
    except DiagnosticAuthError as exc:
        return render(
            request,
            "integrations/diagnostic_authorize.html",
            {
                "authorization_error": (
                    sanitize_text(
                        exc,
                        limit=240,
                    )
                ),
                "fields": fields,
                "client_name": "ChatGPT",
            },
            status=400,
        )

    if request.method == "GET":
        return render(
            request,
            "integrations/diagnostic_authorize.html",
            {
                "fields": fields,
                "client_name": (
                    client.client_name
                    or "ChatGPT"
                ),
                "scopes": [
                    DIAGNOSTICS_SCOPE
                ],
            },
        )

    raw_api_key = request.POST.get(
        "diagnostic_api_key",
        "",
    )
    try:
        raw_code = issue_authorization_code(
            client=client,
            raw_api_key=raw_api_key,
            redirect_uri=fields[
                "redirect_uri"
            ],
            scope=fields["scope"],
            code_challenge=fields[
                "code_challenge"
            ],
            resource=fields["resource"],
        )
    except DiagnosticAuthError as exc:
        return render(
            request,
            "integrations/diagnostic_authorize.html",
            {
                "authorization_error": (
                    sanitize_text(
                        exc,
                        limit=240,
                    )
                ),
                "fields": fields,
                "client_name": (
                    client.client_name
                    or "ChatGPT"
                ),
                "scopes": [
                    DIAGNOSTICS_SCOPE
                ],
            },
            status=403,
        )

    params = {
        "code": raw_code,
        "iss": _issuer(request),
    }
    if fields["state"]:
        params["state"] = fields[
            "state"
        ]

    separator = (
        "&"
        if urlparse(
            fields["redirect_uri"]
        ).query
        else "?"
    )
    return redirect(
        fields["redirect_uri"]
        + separator
        + urlencode(params)
    )


@csrf_exempt
@ratelimit(
    limit=60,
    window=60,
)
@require_POST
def diagnostic_oauth_token(
    request,
):
    grant_type = request.POST.get(
        "grant_type",
        "",
    )
    client_id = request.POST.get(
        "client_id",
        "",
    )
    requested_resource = (
        request.POST.get(
            "resource",
            "",
        )
        or _resource(request)
    )

    try:
        if grant_type == (
            "authorization_code"
        ):
            (
                oauth_token,
                raw_access,
                raw_refresh,
            ) = exchange_authorization_code(
                code=request.POST.get(
                    "code",
                    "",
                ),
                client_id=client_id,
                redirect_uri=(
                    request.POST.get(
                        "redirect_uri",
                        "",
                    )
                ),
                code_verifier=(
                    request.POST.get(
                        "code_verifier",
                        "",
                    )
                ),
                resource=requested_resource,
            )
        elif grant_type == (
            "refresh_token"
        ):
            (
                oauth_token,
                raw_access,
                raw_refresh,
            ) = refresh_access_token(
                refresh_token=(
                    request.POST.get(
                        "refresh_token",
                        "",
                    )
                ),
                client_id=client_id,
                resource=requested_resource,
            )
        else:
            return JsonResponse(
                {
                    "error": (
                        "unsupported_grant_type"
                    ),
                    "error_description": (
                        "Use authorization_code or refresh_token."
                    ),
                },
                status=400,
            )
    except DiagnosticAuthError as exc:
        response = JsonResponse(
            {
                "error": "invalid_grant",
                "error_description": (
                    sanitize_text(
                        exc,
                        limit=240,
                    )
                ),
            },
            status=400,
        )
        response[
            "Cache-Control"
        ] = "no-store"
        return response

    response = JsonResponse(
        {
            "access_token": raw_access,
            "token_type": "Bearer",
            "expires_in": int(
                ACCESS_TOKEN_TTL.total_seconds()
            ),
            "refresh_token": raw_refresh,
            "scope": oauth_token.scope,
            "resource": (
                oauth_token.resource
            ),
        }
    )
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


@csrf_exempt
@ratelimit(
    limit=300,
    window=60,
)
@require_POST
def diagnostic_mcp(request):
    if len(request.body) > (
        1024 * 1024
    ):
        return JsonResponse(
            _jsonrpc_error(
                None,
                -32600,
                "Request body is too large.",
            ),
            status=413,
        )

    try:
        payload = json.loads(
            request.body.decode("utf-8")
            or "{}"
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return JsonResponse(
            _jsonrpc_error(
                None,
                -32700,
                "Parse error.",
            ),
            status=400,
        )

    if (
        not isinstance(payload, dict)
        or payload.get("jsonrpc") != "2.0"
    ):
        return JsonResponse(
            _jsonrpc_error(
                (
                    payload.get("id")
                    if isinstance(
                        payload,
                        dict,
                    )
                    else None
                ),
                -32600,
                "Invalid Request.",
            ),
            status=400,
        )

    request_id = payload.get("id")
    method = str(
        payload.get("method") or ""
    )
    params = (
        payload.get("params")
        if isinstance(
            payload.get("params"),
            dict,
        )
        else {}
    )
    modern = _modern_request(
        request,
        payload,
    )

    try:
        if modern:
            _validate_modern_headers(
                request,
                method=method,
                params=params,
            )
    except DiagnosticToolError as exc:
        return JsonResponse(
            _jsonrpc_error(
                request_id,
                -32020,
                sanitize_text(
                    exc,
                    limit=200,
                ),
                modern=modern,
            ),
            status=400,
        )

    if method == "server/discover":
        result = {
            "supportedVersions": [
                MODERN_PROTOCOL_VERSION
            ],
            "capabilities": {
                "tools": {}
            },
            "instructions": (
                "SHVYA Diagnostics is read-only and organization-scoped. "
                "Use find_leads before lead-specific tools when a lead UUID is "
                "unknown. Never request or infer passwords, access tokens, provider "
                "credentials, internal prompts, or data from another organization."
            ),
            "ttlMs": 300000,
            "cacheScope": "public",
        }
        return JsonResponse(
            _jsonrpc_result(
                request_id,
                result,
                modern=True,
            )
        )

    if method == "initialize":
        requested = str(
            params.get(
                "protocolVersion"
            )
            or LEGACY_PROTOCOL_VERSION
        )
        negotiated = (
            requested
            if requested
            == LEGACY_PROTOCOL_VERSION
            else LEGACY_PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": (
                negotiated
            ),
            "capabilities": {
                "tools": {}
            },
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Read-only, organization-scoped SHVYA diagnostics. "
                "Secrets and internal provider credentials are never exposed."
            ),
        }
        return JsonResponse(
            _jsonrpc_result(
                request_id,
                result,
                modern=False,
            )
        )

    if method == (
        "notifications/initialized"
    ):
        return HttpResponse(
            status=202
        )

    if method == "ping":
        return JsonResponse(
            _jsonrpc_result(
                request_id,
                {},
                modern=modern,
            )
        )

    if method == "tools/list":
        result = {
            "tools": TOOL_DEFINITIONS
        }
        if modern:
            result.update(
                {
                    "ttlMs": 300000,
                    "cacheScope": "public",
                }
            )
        return JsonResponse(
            _jsonrpc_result(
                request_id,
                result,
                modern=modern,
            )
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

    tool_name = str(
        params.get("name") or ""
    )
    arguments = (
        params.get("arguments")
        if isinstance(
            params.get("arguments"),
            dict,
        )
        else {}
    )

    known_tools = {
        item["name"]
        for item in TOOL_DEFINITIONS
    }
    if tool_name not in known_tools:
        return JsonResponse(
            _jsonrpc_error(
                request_id,
                -32602,
                "Unknown diagnostic tool.",
                modern=modern,
            ),
            status=400,
        )

    auth_header = request.headers.get(
        "Authorization",
        "",
    )
    raw_bearer = (
        auth_header[7:].strip()
        if auth_header.lower().startswith(
            "bearer "
        )
        else ""
    )

    if not raw_bearer:
        return _tool_auth_result(
            request,
            request_id,
            modern=modern,
            description=(
                "Connect an organization-scoped SHVYA diagnostic key to continue."
            ),
        )

    try:
        (
            organization,
            api_key,
            oauth_token,
        ) = authenticate_bearer(
            raw_bearer
        )
    except DiagnosticAuthError as exc:
        return _tool_auth_result(
            request,
            request_id,
            modern=modern,
            description=sanitize_text(
                exc,
                limit=160,
            ),
        )

    started = time.perf_counter()
    outcome = (
        DiagnosticAccessLog.Outcome.SUCCESS
    )
    error_code = ""

    try:
        data = execute_tool(
            name=tool_name,
            organization=organization,
            arguments=arguments,
        )
        safe_data = sanitize_data(
            data
        )
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
            "structuredContent": (
                safe_data
            ),
            "isError": False,
        }
    except DiagnosticToolError as exc:
        outcome = (
            DiagnosticAccessLog.Outcome.ERROR
        )
        error_code = (
            "diagnostic_input_error"
        )
        safe_message = sanitize_text(
            exc,
            limit=300,
        )
        result = {
            "content": [
                {
                    "type": "text",
                    "text": safe_message,
                }
            ],
            "structuredContent": {
                "error": safe_message
            },
            "isError": True,
        }
    except Exception:
        outcome = (
            DiagnosticAccessLog.Outcome.ERROR
        )
        error_code = (
            "diagnostic_internal_error"
        )
        result = {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "The diagnostic query failed safely. No provider secret "
                        "or raw internal error was exposed."
                    ),
                }
            ],
            "structuredContent": {
                "error": (
                    "diagnostic_query_failed"
                )
            },
            "isError": True,
        }

    duration_ms = max(
        0,
        int(
            (
                time.perf_counter()
                - started
            )
            * 1000
        ),
    )

    DiagnosticAccessLog.objects.create(
        organization=organization,
        api_key=api_key,
        oauth_client_id=(
            oauth_token.client.client_id
            if oauth_token
            else ""
        ),
        tool_name=tool_name[:100],
        outcome=outcome,
        auth_type=(
            DiagnosticAccessLog.AuthType.OAUTH
            if oauth_token
            else DiagnosticAccessLog.AuthType.API_KEY
        ),
        request_fingerprint=(
            request_fingerprint(
                arguments
            )
        ),
        duration_ms=duration_ms,
        error_code=error_code,
    )

    response = JsonResponse(
        _jsonrpc_result(
            request_id,
            result,
            modern=modern,
        )
    )
    response["Cache-Control"] = "no-store"
    return response
