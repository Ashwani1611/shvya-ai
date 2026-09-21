"""Actor-bound Remote MCP + OAuth endpoints for SHVYA Operations."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from urllib.parse import urlencode, urlparse

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
from apps.integrations.models import OperationsAuditEvent, OperationsSupportSession
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
    revoke_token,
    validate_authorization_request,
)
from apps.integrations.operations_policy import (
    CAPABILITY_LABELS,
    CAP_AI_CONFIG_WRITE,
    CAP_AUDIT_READ,
    CAP_AUTOMATION_CONFIG_WRITE,
    CAP_CRM_CONFIG_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_LEAD_ATTRIBUTES_WRITE,
    CAP_LEAD_STAGE_WRITE,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    effective_capabilities,
    policy_for,
)
from apps.integrations.operations_tools import (
    DIAGNOSTIC_TOOL_NAMES,
    OperationsApprovalRequired,
    OperationsPermissionError,
    OperationsToolError,
    execute_operations_tool,
)
from apps.integrations.views.mcp import TOOL_DEFINITIONS as DIAGNOSTIC_TOOL_DEFINITIONS

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
SERVER_INFO = {"name": "shvya-operations", "version": "1.0.0"}

OAUTH_SCHEMES = [
    {
        "type": "oauth2",
        "scopes": [
            OPERATIONS_READ_SCOPE,
            OPERATIONS_WRITE_SCOPE,
            OFFLINE_SCOPE,
        ],
    }
]




def _issuer(request):
    return request.build_absolute_uri("/operations/").rstrip("/")


def _resource(request):
    return request.build_absolute_uri(reverse("shvya-operations-mcp"))


def _resource_metadata_url(request):
    return request.build_absolute_uri(
        reverse("shvya-operations-oauth-resource-metadata")
    )


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
        str((params or {}).get("name") or "") if method == "tools/call" else ""
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
        _jsonrpc_result(request_id, result, modern=modern)
    )
    response["WWW-Authenticate"] = challenge
    response["Cache-Control"] = "no-store"
    return response


def _write_properties(extra=None):
    properties = {
        "dry_run": {
            "type": "boolean",
            "default": True,
            "description": "Preview the exact change without applying it.",
        },
        "approved": {
            "type": "boolean",
            "default": False,
            "description": "Set true only after the human approved an approval-required dry-run.",
        },
        "approval_event_id": {
            "type": "string",
            "format": "uuid",
            "description": (
                "Immutable dry-run audit event ID returned by SHVYA. Required "
                "with approved=true when the dry-run said approval_required=true."
            ),
        },
        "reason": {
            "type": "string",
            "minLength": 8,
            "maxLength": 500,
            "description": "Specific operational reason for the proposed mutation.",
        },
    }
    properties.update(extra or {})
    return properties


def _tool(
    name,
    title,
    description,
    properties=None,
    required=None,
    *,
    read_only=True,
):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "openWorldHint": False,
        },
        "securitySchemes": OAUTH_SCHEMES,
    }


OWN_TOOL_DEFINITIONS = [
    _tool(
        "get_operations_context",
        "Get SHVYA Operations context",
        "Return the authenticated SHVYA role, human actor, active organization context, and effective capabilities.",
    ),
    _tool(
        "list_organizations",
        "List SHVYA organizations",
        "Superadmin only. Find organizations that may be selected as an explicit support context.",
        {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        },
    ),
    _tool(
        "select_organization_context",
        "Select organization support context",
        "Superadmin only. Enter one explicit customer support context. This creates an organization-visible SHVYA Support session.",
        {
            "organization_id": {"type": "string", "format": "uuid"},
            "reason": {
                "type": "string",
                "minLength": 8,
                "maxLength": 500,
                "description": "Specific support reason for selecting this organization.",
            },
        },
        ["organization_id", "reason"],
        read_only=False,
    ),
    _tool(
        "clear_organization_context",
        "Leave organization support context",
        "Superadmin only. End the active customer support context and its visible support session.",
        {
            "reason": {
                "type": "string",
                "minLength": 8,
                "maxLength": 500,
                "description": "Specific reason for ending the active support context.",
            },
        },
        ["reason"],
        read_only=False,
    ),
    _tool(
        "get_organization_configuration",
        "Understand SHVYA organization",
        "Inspect business/AI configuration, pipelines, stages, attributes, Playbook qualification, Workflows and Cadence counts for the active organization.",
    ),
    _tool(
        "get_automation_configuration",
        "Inspect Workflows and Cadence",
        "Return tenant-scoped Workflow definitions and Cadence/step configuration needed to diagnose or safely edit automation. Secrets and provider credentials are not returned.",
        {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 50,
            }
        },
    ),
    _tool(
        "diagnose_lead_qualification",
        "Diagnose lead qualification",
        "Explain why one organization-scoped lead did or did not reach Qualified using persisted qualification and CRM evidence.",
        {"lead_id": {"type": "string", "format": "uuid"}},
        ["lead_id"],
    ),
    _tool(
        "get_conversion_analysis",
        "Analyze conversion change",
        "Compare equivalent current/previous periods using persisted lead volume, Qualified transition activity and workflow failures without claiming unsupported causality.",
        {
            "days": {
                "type": "integer",
                "minimum": 7,
                "maximum": 90,
                "default": 30,
            }
        },
    ),
    _tool(
        "move_lead_stage",
        "Move one lead safely",
        "Dry-run or move one lead to an active organization-owned stage through SHVYA's canonical pipeline/stage transition service.",
        _write_properties(
            {
                "lead_id": {"type": "string", "format": "uuid"},
                "target_stage_id": {"type": "string", "format": "uuid"},
            }
        ),
        ["lead_id", "target_stage_id", "reason"],
        read_only=False,
    ),
    _tool(
        "repair_qualification_stage",
        "Repair completed qualification stage",
        "Dry-run or reconcile a lead to its active Qualified stage only when persisted backend qualification state is already completed.",
        _write_properties(
            {"lead_id": {"type": "string", "format": "uuid"}}
        ),
        ["lead_id", "reason"],
        read_only=False,
    ),
    _tool(
        "update_lead_attributes",
        "Update lead attributes",
        "Dry-run or update existing non-sensitive organization-defined lead attributes. Credential-like fields are blocked.",
        _write_properties(
            {
                "lead_id": {"type": "string", "format": "uuid"},
                "values": {"type": "object"},
            }
        ),
        ["lead_id", "values", "reason"],
        read_only=False,
    ),
    _tool(
        "update_ai_configuration",
        "Update organization AI configuration",
        "Dry-run or update the organization AI profile/Playbook. Backend qualification, tenant, evidence and CRM execution rules remain authoritative.",
        _write_properties(
            {
                "changes": {
                    "type": "object",
                    "properties": {
                        "about": {"type": "string"},
                        "bot_languages": {"type": "string"},
                        "ai_playbook": {"type": "string"},
                        "ai_enabled": {"type": "boolean"},
                        "bump_up_enabled": {"type": "boolean"},
                        "bump_up_count": {"type": "integer", "minimum": 0, "maximum": 20},
                    },
                    "additionalProperties": False,
                }
            }
        ),
        ["changes", "reason"],
        read_only=False,
    ),
    _tool(
        "upsert_pipeline_configuration",
        "Configure CRM pipeline",
        "Dry-run or create/update one tenant-owned CRM pipeline. New pipelines receive SHVYA's standard stages.",
        _write_properties(
            {
                "pipeline_id": {"type": "string", "format": "uuid"},
                "data": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "is_active": {"type": "boolean"},
                        "ai_enabled": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "upsert_stage_configuration",
        "Configure CRM stage",
        "Dry-run or create/update one active-pipeline stage. SHVYA protected stages cannot be renamed or deactivated.",
        _write_properties(
            {
                "pipeline_id": {"type": "string", "format": "uuid"},
                "stage_id": {"type": "string", "format": "uuid"},
                "data": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "display_order": {"type": "integer", "minimum": 0},
                        "is_active": {"type": "boolean"},
                        "ai_on": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            }
        ),
        ["pipeline_id", "data", "reason"],
        read_only=False,
    ),
    _tool(
        "upsert_attribute_configuration",
        "Configure CRM attribute",
        "Dry-run or create/update an organization custom attribute through SHVYA's attribute service. Secret/credential-like definitions are blocked.",
        _write_properties(
            {
                "attribute_id": {"type": "string", "format": "uuid"},
                "data": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "field_type": {
                            "type": "string",
                            "enum": ["text", "numeric", "date", "datetime", "option"],
                        },
                        "description": {"type": "string"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "additionalProperties": False,
                },
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "upsert_workflow_configuration",
        "Configure Workflow",
        "Dry-run or create/update one organization Workflow using SHVYA's canonical Workflow validator. Pipeline, stage, source, attribute, account and action references remain tenant validated.",
        _write_properties(
            {
                "workflow_id": {"type": "string", "format": "uuid"},
                "data": {"type": "object"},
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "upsert_cadence_configuration",
        "Configure Cadence",
        "Dry-run or create/update a tenant-owned Cadence using SHVYA's canonical follow-up service.",
        _write_properties(
            {
                "cadence_id": {"type": "string", "format": "uuid"},
                "data": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "provider": {"type": "string", "enum": ["api", "hosted"]},
                        "whatsapp_account_id": {"type": "string", "format": "uuid"},
                    },
                    "additionalProperties": False,
                },
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "add_cadence_step",
        "Add Cadence step",
        "Dry-run or append one WhatsApp-template, email or reminder step to an active Cadence. Existing schedule and template validation is reused.",
        _write_properties(
            {
                "cadence_id": {"type": "string", "format": "uuid"},
                "data": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["whatsapp", "email", "reminder"],
                        },
                        "template_id": {"type": "string", "format": "uuid"},
                        "title": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "text": {"type": "string"},
                        "retry_count": {"type": "integer", "minimum": 0, "maximum": 5},
                        "schedule": {"type": "object"},
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
            }
        ),
        ["cadence_id", "data", "reason"],
        read_only=False,
    ),
    _tool(
        "get_operations_audit",
        "Review SHVYA Operations audit",
        "Return safe tenant-scoped audit events for external-AI and Superadmin Operations actions.",
        {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 30,
            }
        },
    ),
]

DIAGNOSTIC_DEFINITIONS = []
for definition in DIAGNOSTIC_TOOL_DEFINITIONS:
    if definition["name"] not in DIAGNOSTIC_TOOL_NAMES:
        continue
    item = deepcopy(definition)
    item["securitySchemes"] = OAUTH_SCHEMES
    item.pop("_meta", None)
    DIAGNOSTIC_DEFINITIONS.append(item)

TOOL_DEFINITIONS = OWN_TOOL_DEFINITIONS + DIAGNOSTIC_DEFINITIONS
KNOWN_TOOLS = {item["name"] for item in TOOL_DEFINITIONS}

TOOL_CAPABILITIES = {
    "get_operations_context": None,
    "list_organizations": None,
    "select_organization_context": None,
    "clear_organization_context": None,
    "get_organization_configuration": CAP_ORGANIZATION_READ,
    "get_automation_configuration": CAP_ORGANIZATION_READ,
    "diagnose_lead_qualification": CAP_DIAGNOSTICS_READ,
    "get_conversion_analysis": CAP_DIAGNOSTICS_READ,
    "move_lead_stage": CAP_LEAD_STAGE_WRITE,
    "repair_qualification_stage": CAP_LEAD_STAGE_WRITE,
    "update_lead_attributes": CAP_LEAD_ATTRIBUTES_WRITE,
    "update_ai_configuration": CAP_AI_CONFIG_WRITE,
    "upsert_pipeline_configuration": CAP_CRM_CONFIG_WRITE,
    "upsert_stage_configuration": CAP_CRM_CONFIG_WRITE,
    "upsert_attribute_configuration": CAP_CRM_CONFIG_WRITE,
    "upsert_workflow_configuration": CAP_AUTOMATION_CONFIG_WRITE,
    "upsert_cadence_configuration": CAP_AUTOMATION_CONFIG_WRITE,
    "add_cadence_step": CAP_AUTOMATION_CONFIG_WRITE,
    "get_operations_audit": CAP_AUDIT_READ,
}
for _diagnostic_name in DIAGNOSTIC_TOOL_NAMES:
    TOOL_CAPABILITIES[_diagnostic_name] = CAP_DIAGNOSTICS_READ


def _tools_for_identity(identity):
    """Return only tools discoverable to the authenticated SHVYA identity."""

    if identity.role == ROLE_SUPERADMIN:
        return TOOL_DEFINITIONS

    organization = identity.organization
    capabilities = effective_capabilities(
        role=identity.role,
        organization=organization,
    )
    visible = []
    for item in TOOL_DEFINITIONS:
        name = item["name"]
        if name == "get_operations_context":
            visible.append(item)
            continue
        # Tenant-switch/platform-discovery tools are Superadmin-only.
        if name in {
            "list_organizations",
            "select_organization_context",
            "clear_organization_context",
        }:
            continue
        capability = TOOL_CAPABILITIES.get(name)
        if capability is None or capability in capabilities:
            visible.append(item)
    return visible


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
            "resource_documentation": request.build_absolute_uri(
                reverse("crm-connect-hub-shvya-api")
            ),
        }
    )


@require_GET
def operations_oauth_server_metadata(request):
    return JsonResponse(
        {
            "issuer": _issuer(request),
            "authorization_endpoint": request.build_absolute_uri(
                reverse("shvya-operations-oauth-authorize")
            ),
            "token_endpoint": request.build_absolute_uri(
                reverse("shvya-operations-oauth-token")
            ),
            "registration_endpoint": request.build_absolute_uri(
                reverse("shvya-operations-oauth-register")
            ),
            "revocation_endpoint": request.build_absolute_uri(
                reverse("shvya-operations-oauth-revoke")
            ),
            "revocation_endpoint_auth_methods_supported": ["none"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
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
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid_client_metadata"}, status=400)

    if payload.get("token_endpoint_auth_method", "none") != "none":
        return JsonResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "SHVYA Operations supports public PKCE clients only.",
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
        )
    except OperationsAuthError as exc:
        return JsonResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": sanitize_text(exc, limit=240),
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
        "state": source.get("state", ""),
        "resource": source.get("resource", _resource(request)),
    }


@ratelimit(limit=30, window=60)
@require_http_methods(["GET", "POST"])
def operations_oauth_authorize(request):
    fields = _authorization_fields(request)
    try:
        client = validate_authorization_request(
            client_id=fields["client_id"],
            redirect_uri=fields["redirect_uri"],
            response_type=fields["response_type"],
            code_challenge=fields["code_challenge"],
            code_challenge_method=fields["code_challenge_method"],
            scope=fields["scope"],
        )
        if fields["resource"] != _resource(request):
            raise OperationsAuthError(
                "OAuth resource does not match the SHVYA Operations MCP endpoint."
            )
    except OperationsAuthError as exc:
        return render(
            request,
            "integrations/operations_authorize.html",
            {
                "authorization_error": sanitize_text(exc, limit=240),
                "fields": fields,
                "client_name": "External AI",
                "identities": {},
            },
            status=400,
        )

    identities = available_browser_identities(request)
    identity_options = []
    for role, actor in identities.items():
        organization = (
            None if role == ROLE_SUPERADMIN else actor.organization
        )
        capabilities = sorted(
            effective_capabilities(
                role=role,
                organization=organization,
            )
        )
        policy = (
            policy_for(organization)
            if organization is not None
            else None
        )
        approval_values = set(
            policy.approval_required_capabilities or []
        ) if policy is not None else set()
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
                            capability.endswith(".write")
                            and (
                                role == ROLE_SUPERADMIN
                                or capability in approval_values
                            )
                        ),
                    }
                    for capability in capabilities
                ],
            }
        )

    requested_scopes = sorted(
        set(str(fields.get("scope") or "").split())
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
        context["authorization_error"] = (
            "The selected SHVYA role is not currently authenticated or is not enabled for Operations MCP."
        )
        return render(
            request,
            "integrations/operations_authorize.html",
            context,
            status=403,
        )
    try:
        raw_code = issue_authorization_code(
            client=client,
            actor=actor,
            role=role,
            redirect_uri=fields["redirect_uri"],
            scope=fields["scope"],
            code_challenge=fields["code_challenge"],
            resource=fields["resource"],
        )
    except OperationsAuthError as exc:
        context["authorization_error"] = sanitize_text(exc, limit=240)
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
    grant_type = request.POST.get("grant_type", "")
    client_id = request.POST.get("client_id", "")
    requested_resource = request.POST.get("resource", "") or _resource(request)
    try:
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
            return JsonResponse(
                {
                    "error": "unsupported_grant_type",
                    "error_description": "Use authorization_code or refresh_token.",
                },
                status=400,
            )
    except OperationsAuthError as exc:
        response = JsonResponse(
            {
                "error": "invalid_grant",
                "error_description": sanitize_text(exc, limit=240),
            },
            status=400,
        )
        response["Cache-Control"] = "no-store"
        return response

    response = JsonResponse(
        {
            "access_token": raw_access,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "refresh_token": raw_refresh,
            "scope": token.scope,
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
    organization = None
    if execution and execution.target_type == "organization" and execution.target_id:
        # Use the authenticated context rather than trusting target IDs.
        organization = (
            identity.token.active_organization
            if identity.role == ROLE_SUPERADMIN
            else identity.organization
        )
    else:
        organization = (
            identity.token.active_organization
            if identity.role == ROLE_SUPERADMIN
            else identity.organization
        )

    support_session = None
    if identity.role == ROLE_SUPERADMIN and organization is not None:
        support_session = (
            OperationsSupportSession.objects.filter(
                token=identity.token,
                organization=organization,
                ended_at__isnull=True,
            )
            .order_by("-started_at")
            .first()
        )
        if support_session is not None:
            OperationsSupportSession.objects.filter(pk=support_session.pk).update(
                last_seen_at=timezone.now()
            )

    safe_summary = sanitize_data(
        (execution.audit_summary if execution else {}) or {}
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

    request_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    modern = _modern_request(request, payload)
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
                    "supportedVersions": [MODERN_PROTOCOL_VERSION],
                    "capabilities": {"tools": {}},
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
                    "capabilities": {"tools": {}},
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
            result["_meta"] = {
                "shvya/role": identity.role,
                "shvya/effective_tool_count": len(tools),
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
    arguments = (
        params.get("arguments")
        if isinstance(params.get("arguments"), dict)
        else {}
    )
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

    started = time.perf_counter()
    execution = None
    error_code = ""
    audit_outcome = None
    error_reason = ""
    try:
        execution = execute_operations_tool(
            name=tool_name,
            identity=identity,
            arguments=arguments,
        )
        safe_data = sanitize_data(execution.data)
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

    response = JsonResponse(
        _jsonrpc_result(request_id, result, modern=modern)
    )
    response["Cache-Control"] = "no-store"
    return response
