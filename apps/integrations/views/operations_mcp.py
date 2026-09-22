"""Actor-bound Remote MCP + OAuth endpoints for SHVYA Operations."""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
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
from apps.integrations.operations_policy import (
    CAPABILITY_LABELS,
    CAP_AI_CONFIG_WRITE,
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_AUDIT_READ,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_CONFIGURATION_PLAN_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_LEAD_ATTRIBUTES_WRITE,
    CAP_LEAD_STAGE_WRITE,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    WRITE_CAPABILITIES,
    approval_required,
    effective_capabilities,
)
from apps.integrations.operations_tools import (
    DIAGNOSTIC_TOOL_NAMES,
    OperationsApprovalRequired,
    OperationsManualFixRequired,
    OperationsPermissionError,
    OperationsSuperadminRequired,
    OperationsToolError,
    execute_operations_tool,
)
from apps.integrations.views.mcp import TOOL_DEFINITIONS as DIAGNOSTIC_TOOL_DEFINITIONS

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
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
}

OAUTH_READ_SCHEMES = [
    {
        "type": "oauth2",
        "scopes": [
            OPERATIONS_READ_SCOPE,
            OFFLINE_SCOPE,
        ],
    }
]

OAUTH_WRITE_SCHEMES = [
    {
        "type": "oauth2",
        "scopes": [
            OPERATIONS_READ_SCOPE,
            OPERATIONS_WRITE_SCOPE,
            OFFLINE_SCOPE,
        ],
    }
]




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
    return request.build_absolute_uri("/operations/").rstrip("/")


def _resource(request):
    return request.build_absolute_uri(reverse("shvya-operations-mcp"))


def _resource_metadata_url(request):
    return request.build_absolute_uri(
        reverse("shvya-operations-oauth-resource-metadata-rfc9728")
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
        _jsonrpc_result(request_id, result, modern=modern),
        status=401,
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
    requires_write_scope=None,
    destructive=False,
):
    security_schemes = (
        OAUTH_WRITE_SCHEMES
        if (
            (not read_only)
            if requires_write_scope is None
            else requires_write_scope
        )
        else OAUTH_READ_SCHEMES
    )
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
            "destructiveHint": destructive,
            "openWorldHint": False,
        },
        "securitySchemes": security_schemes,
        "_meta": {
            "securitySchemes": deepcopy(security_schemes),
        },
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
        requires_write_scope=False,
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
        requires_write_scope=False,
    ),
    _tool(
        "get_organization_configuration",
        "Understand SHVYA organization",
        "Inspect business/AI configuration, pipelines, stages, attributes, Playbook qualification, Workflows and Cadence counts for the active organization.",
    ),
    _tool(
        "get_ai_configuration",
        "Inspect full SHVYA AI configuration",
        "Return the complete organization AI profile and Playbook up to SHVYA's canonical stored limits, with credential-like text redacted and explicit redaction flags. Use this before replacing a Playbook when the organization summary says the excerpt is truncated.",
    ),
    _tool(
        "get_knowledge_health",
        "Inspect SHVYA knowledge health",
        "Return bounded organization knowledge/RAG metadata only: source types/names, URL hostnames, document version/status/publication state, chunk/embedding coverage and timestamps. Document text, file bytes, raw signed URLs and vectors are never returned.",
        {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 50,
            },
        },
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
        "get_messaging_automation_settings",
        "Inspect messaging automation settings",
        "Return only canonical pipeline-linked WhatsApp automation controls for one or all active organization accounts, including AI auto-reply, lead creation, bump-up, follow-up, business hours, and active-conversation delay. Provider credentials and legacy/internal settings keys are never returned.",
        {
            "whatsapp_account_id": {
                "type": "string",
                "format": "uuid",
            },
        },
    ),
    _tool(
        "update_messaging_automation_settings",
        "Configure messaging automation settings",
        "Dry-run or update one active organization WhatsApp account's canonical pipeline-linked automation settings. Existing SHVYA pipeline mapping, scheduler, timing, and AI-permission rules remain authoritative.",
        _write_properties(
            {
                "whatsapp_account_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "changes": {
                    "type": "object",
                    "properties": {
                        "ai_auto_reply": {"type": "boolean"},
                        "auto_lead_creation": {"type": "boolean"},
                        "bump_up_messages": {"type": "boolean"},
                        "bump_up_count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                        },
                        "auto_follow_up": {"type": "boolean"},
                        "business_hours_start": {
                            "type": "string",
                            "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
                        },
                        "business_hours_end": {
                            "type": "string",
                            "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
                        },
                        "active_conversation_delay_value": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 168,
                        },
                        "active_conversation_delay_unit": {
                            "type": "string",
                            "enum": ["minutes", "hours", "days"],
                        },
                    },
                    "additionalProperties": False,
                },
            }
        ),
        ["whatsapp_account_id", "changes", "reason"],
        read_only=False,
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
        "Compare equivalent current/previous periods using persisted lead/source/stage mix, first-response timing, follow-up and campaign outcomes, Qualified/lost transitions, captured lost reasons, messaging/AI/Workflow failures, plus a current lead-ageing snapshot without claiming unsupported causality.",
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
        "Dry-run or move one lead to an active organization-owned stage through SHVYA's canonical transition service. Target-stage required attributes are enforced, and Qualified remains backend qualification-contract owned.",
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
        "Dry-run or reconcile a lead to SHVYA's authoritative qualification completion target only when backend qualification is completed and configured criteria are satisfied.",
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
                        "about": {
                            "type": "string",
                            "maxLength": 12000,
                        },
                        "bot_languages": {
                            "type": "string",
                            "maxLength": 500,
                        },
                        "ai_playbook": {
                            "type": "string",
                            "maxLength": 100000,
                        },
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
        "Dry-run or create/update a tenant-owned Cadence using SHVYA's canonical follow-up service. Existing Cadence sender/provider cannot be changed; create a new Cadence for a different sender/provider.",
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
        "get_qualification_configuration",
        "Inspect qualification configuration",
        "Return the compiled qualification requirements, conditional eligibility, mappings, completion target, final acknowledgment, and configuration diagnostics.",
    ),
    _tool(
        "validate_qualification_configuration",
        "Validate qualification configuration",
        "Compile and validate a proposed structured qualification configuration without writing it.",
        {
            "data": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["configured", "all_required", "majority"]},
                    "requirements": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 30,
                        "items": {
                            "type": "object",
                            "properties": {
                                "stable_id": {"type": "string", "maxLength": 64},
                                "question": {"type": "string", "maxLength": 2000},
                                "required": {"type": "boolean"},
                                "options": {
                                    "type": "array",
                                    "maxItems": 30,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "key": {"type": "string", "maxLength": 10},
                                            "value": {"type": "string", "maxLength": 500},
                                        },
                                        "required": ["key", "value"],
                                        "additionalProperties": False,
                                    },
                                },
                                "eligible_when": {
                                    "type": "object",
                                    "properties": {
                                        "requirement_id": {"type": "string"},
                                        "operator": {"type": "string", "enum": ["eq"]},
                                        "value": {"type": ["string", "number", "boolean"]},
                                    },
                                    "required": ["requirement_id", "value"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["stable_id", "question"],
                            "additionalProperties": False,
                        },
                    },
                    "mappings": {
                        "type": ["array", "object"],
                    },
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 2000},
                        "minItems": 1,
                        "maxItems": 30,
                    },
                    "target_stage_id": {"type": "string", "format": "uuid"},
                    "final_ack": {"type": "string", "maxLength": 2000},
                },
                "required": ["requirements", "target_stage_id", "final_ack"],
                "additionalProperties": False,
            },
        },
        ["data"],
    ),
    _tool(
        "upsert_qualification_configuration",
        "Configure qualification",
        "Dry-run or atomically replace the qualification-owned sections of the canonical AI Playbook while preserving unrelated Playbook sections.",
        _write_properties(
            {
                "data": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["configured", "all_required", "majority"]},
                        "requirements": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "object"}},
                        "mappings": {"type": ["array", "object"]},
                        "criteria": {"type": "array", "items": {"type": "string"}},
                        "target_stage_id": {"type": "string", "format": "uuid"},
                        "final_ack": {"type": "string", "maxLength": 2000},
                    },
                    "required": ["requirements", "target_stage_id", "final_ack"],
                    "additionalProperties": False,
                }
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "list_whatsapp_accounts",
        "List WhatsApp accounts",
        "Return safe organization WhatsApp account identity, status, and pipeline routing only. Credentials and session secrets are never returned.",
    ),
    _tool(
        "validate_whatsapp_routing",
        "Validate WhatsApp routing",
        "Audit active WhatsApp accounts against active pipeline-bound phone routing and report duplicates or unbound accounts without exposing credentials.",
    ),
    _tool(
        "bind_whatsapp_account_to_pipeline",
        "Bind WhatsApp routing to pipeline",
        "Dry-run or bind a WhatsApp number to one active pipeline using SHVYA's pipeline-bound routing invariant. May be used before Hosted connection creation.",
        _write_properties(
            {
                "pipeline_id": {"type": "string", "format": "uuid"},
                "whatsapp_account_id": {"type": "string", "format": "uuid"},
                "country_code": {"type": "string", "maxLength": 10},
                "phone_number": {"type": "string", "maxLength": 32},
            }
        ),
        ["pipeline_id", "reason"],
        read_only=False,
    ),
    _tool(
        "begin_whatsapp_connection",
        "Begin Hosted WhatsApp connection",
        "Dry-run or create/restart a Hosted linked-device WhatsApp session for a number already bound to an active pipeline. Returns safe status only; QR/session credentials are never exposed through MCP.",
        _write_properties(
            {
                "country_code": {"type": "string", "maxLength": 10},
                "phone_number": {"type": "string", "maxLength": 32},
            }
        ),
        ["country_code", "phone_number", "reason"],
        read_only=False,
    ),
    _tool(
        "list_workflow_triggers",
        "List Workflow triggers",
        "Return every canonical Workflow trigger and its typed conditions schema.",
    ),
    _tool(
        "list_workflow_actions",
        "List Workflow actions",
        "Return every canonical Workflow action and its typed action schema.",
    ),
    _tool(
        "get_workflow_schema",
        "Get Workflow schema",
        "Return typed canonical trigger/action schemas plus tenant-safe reference catalogs so agents do not guess Workflow payloads.",
        {
            "trigger_type": {"type": "string"},
            "action_type": {"type": "string"},
        },
    ),
    _tool(
        "validate_workflow_configuration",
        "Validate Workflow configuration",
        "Validate and normalize one proposed Workflow through SHVYA's canonical Workflow validator without saving or executing it.",
        {"data": {"type": "object"}},
        ["data"],
    ),
    _tool(
        "list_touchpoints",
        "List Touchpoints",
        "Return organization saved replies grouped by category.",
        {"include_archived": {"type": "boolean", "default": False}},
    ),
    _tool(
        "upsert_touchpoint",
        "Configure Touchpoint",
        "Dry-run or create/update one organization saved reply. Existing category records are reused where possible.",
        _write_properties(
            {
                "touchpoint_id": {"type": "string", "format": "uuid"},
                "data": {
                    "type": "object",
                    "properties": {
                        "category_id": {"type": "string", "format": "uuid"},
                        "category_name": {"type": "string", "maxLength": 100},
                        "title": {"type": "string", "maxLength": 150},
                        "body": {"type": "string", "maxLength": 1000},
                    },
                    "required": ["title", "body"],
                    "additionalProperties": False,
                },
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "archive_touchpoint",
        "Archive Touchpoint",
        "Dry-run or archive one saved reply. Archiving hides it from normal Cadence/contact-panel use while retaining the record.",
        _write_properties(
            {"touchpoint_id": {"type": "string", "format": "uuid"}}
        ),
        ["touchpoint_id", "reason"],
        read_only=False,
    ),
    _tool(
        "list_faqs",
        "List FAQs",
        "Return organization FAQs independently from the AI Playbook.",
        {"active_only": {"type": "boolean", "default": False}},
    ),
    _tool(
        "upsert_faq",
        "Configure FAQ",
        "Dry-run or create/update one organization FAQ through the canonical FAQ service.",
        _write_properties(
            {
                "faq_id": {"type": "string"},
                "data": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "answer": {"type": "string"},
                        "is_active": {"type": "boolean"},
                    },
                    "required": ["question", "answer"],
                    "additionalProperties": False,
                },
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "archive_faq",
        "Archive FAQ",
        "Dry-run or deactivate one FAQ while retaining its history.",
        _write_properties({"faq_id": {"type": "string"}}),
        ["faq_id", "reason"],
        read_only=False,
    ),
    _tool(
        "create_knowledge_source",
        "Create knowledge source",
        "Dry-run or create an organization URL knowledge source and optionally queue canonical ingestion/indexing.",
        _write_properties(
            {
                "data": {
                    "type": "object",
                    "properties": {
                        "source_type": {"type": "string", "enum": ["url"]},
                        "name": {"type": "string", "maxLength": 255},
                        "url": {"type": "string", "format": "uri"},
                        "ingest": {"type": "boolean", "default": True},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                }
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "upload_knowledge_document",
        "Upload knowledge document",
        "Dry-run or upload a base64 knowledge file through SHVYA's existing file security/quota validation, then queue canonical ingestion/indexing.",
        _write_properties(
            {
                "data": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "maxLength": 255},
                        "name": {"type": "string", "maxLength": 255},
                        "content_base64": {"type": "string"},
                    },
                    "required": ["filename", "content_base64"],
                    "additionalProperties": False,
                }
            }
        ),
        ["data", "reason"],
        read_only=False,
    ),
    _tool(
        "publish_knowledge_document",
        "Publish knowledge document",
        "Dry-run or publish a completed fully embedded organization knowledge document through the canonical version publisher.",
        _write_properties({"document_id": {"type": "string"}}),
        ["document_id", "reason"],
        read_only=False,
    ),
    _tool(
        "archive_knowledge_document",
        "Archive knowledge document",
        "Dry-run or deactivate a knowledge document and its active chunks without deleting file/history.",
        _write_properties({"document_id": {"type": "string"}}),
        ["document_id", "reason"],
        read_only=False,
    ),
    _tool(
        "add_hosted_whatsapp_step",
        "Add Hosted WhatsApp Cadence step",
        "Dry-run or add a free-form Hosted WhatsApp message step using SHVYA's existing Hosted automation service. Optional base64 media is validated and stored through the canonical attachment rules.",
        _write_properties(
            {
                "cadence_id": {"type": "string", "format": "uuid"},
                "data": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "maxLength": 255},
                        "body": {"type": "string", "maxLength": 20000},
                        "schedule": {"type": "object"},
                        "attachment_name": {"type": "string", "maxLength": 255},
                        "attachment_mime_type": {"type": "string", "maxLength": 120},
                        "attachment_base64": {"type": "string"},
                    },
                    "required": ["title", "body"],
                    "additionalProperties": False,
                },
            }
        ),
        ["cadence_id", "data", "reason"],
        read_only=False,
    ),
    _tool(
        "update_cadence_step",
        "Update Cadence step",
        "Dry-run or update an existing Cadence step and its schedule. Hosted free-form content remains Hosted-only and API WhatsApp steps remain approved-template-only.",
        _write_properties(
            {
                "cadence_id": {"type": "string", "format": "uuid"},
                "step_id": {"type": "string", "format": "uuid"},
                "data": {"type": "object"},
            }
        ),
        ["cadence_id", "step_id", "data", "reason"],
        read_only=False,
    ),
    _tool(
        "delete_cadence_step",
        "Delete Cadence step",
        "Dry-run or permanently delete one Cadence step only when it has no delivery execution history. Historical steps fail closed.",
        _write_properties(
            {
                "cadence_id": {"type": "string", "format": "uuid"},
                "step_id": {"type": "string", "format": "uuid"},
            }
        ),
        ["cadence_id", "step_id", "reason"],
        read_only=False,
        destructive=True,
    ),
    _tool(
        "reorder_cadence_steps",
        "Reorder Cadence steps",
        "Dry-run or atomically reorder all steps in one Cadence. The supplied list must contain every current step exactly once.",
        _write_properties(
            {
                "cadence_id": {"type": "string", "format": "uuid"},
                "step_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "minItems": 1,
                },
            }
        ),
        ["cadence_id", "step_ids", "reason"],
        read_only=False,
    ),
    _tool(
        "simulate_ai_conversation",
        "Simulate AI qualification",
        "Run a deterministic no-side-effect qualification simulation against the current Playbook using synthetic answers. It does not create a lead or send a message.",
        {
            "answers": {"type": "object"},
        },
    ),
    _tool(
        "simulate_workflow",
        "Simulate Workflow",
        "Evaluate a stored or proposed Workflow against one organization lead and synthetic event payload without creating a TriggerRun or executing actions.",
        {
            "lead_id": {"type": "string", "format": "uuid"},
            "workflow_id": {"type": "string", "format": "uuid"},
            "data": {"type": "object"},
            "event": {"type": "object"},
        },
        ["lead_id"],
    ),
    _tool(
        "simulate_cadence",
        "Simulate Cadence",
        "Calculate ordered Cadence timing through the canonical scheduler/business-hours logic without sending messages or creating lead sequence state.",
        {
            "cadence_id": {"type": "string", "format": "uuid"},
            "reference_at": {"type": "string"},
        },
        ["cadence_id"],
    ),
    _tool(
        "get_configuration_dependency_graph",
        "Inspect configuration dependencies",
        "Return tenant-safe object dependencies, affected-record counts, protected-stage state, and per-object/configuration ETags.",
        {
            "object_type": {
                "type": "string",
                "enum": ["pipeline", "stage", "attribute", "workflow", "cadence"],
            },
            "object_id": {"type": "string"},
        },
    ),
    _tool(
        "validate_organization_configuration",
        "Validate organization configuration",
        "Audit qualification, WhatsApp routing, Workflows, Cadence ordering/content, stage ordering, and common configuration loops/conflicts without changing state.",
    ),
    _tool(
        "reorder_stages",
        "Reorder pipeline stages",
        "Dry-run or atomically reorder every stage in one pipeline. The supplied ordered list must contain every current stage exactly once.",
        _write_properties(
            {
                "pipeline_id": {"type": "string", "format": "uuid"},
                "stage_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "minItems": 1,
                },
            }
        ),
        ["pipeline_id", "stage_ids", "reason"],
        read_only=False,
    ),
    _tool(
        "export_organization_configuration",
        "Export organization configuration",
        "Return a portable, secret-free organization configuration package with CRM, AI, Cadence, Workflow, Touchpoint, FAQ and messaging settings. Lead/message history, credentials and knowledge binaries are excluded.",
    ),
    _tool(
        "create_configuration_plan",
        "Create atomic configuration plan",
        "Validate and persist one actor/tenant/OAuth-bound multi-object configuration plan, return the complete diff/risk summary and one approval receipt. Refer to prior plan results with objects like {\"$ref\":\"pipeline_sales.target_id\"}.",
        {
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 200,
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "maxLength": 80},
                        "tool": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["tool", "arguments"],
                    "additionalProperties": False,
                },
            },
            "reason": {"type": "string", "minLength": 8, "maxLength": 500},
            "idempotency_key": {"type": "string", "maxLength": 128},
            "ttl_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
        },
        ["operations", "reason"],
        read_only=False,
        requires_write_scope=True,
    ),
    _tool(
        "apply_configuration_plan",
        "Apply approved configuration plan",
        "Apply an approved configuration plan in one database transaction. Any member failure rolls back the entire plan; a stale base ETag fails closed before writes.",
        {
            "plan_id": {"type": "string", "format": "uuid"},
            "approved": {"type": "boolean"},
            "approval_event_id": {"type": "string", "format": "uuid"},
            "reason": {"type": "string", "minLength": 8, "maxLength": 500},
        },
        ["plan_id", "reason"],
        read_only=False,
        requires_write_scope=True,
    ),
    _tool(
        "rollback_configuration_plan",
        "Rollback recoverable configuration plan",
        "Dry-run or roll back an applied update-only plan when no newer configuration drift exists. Plans containing creations remain atomic on failed apply but are not later auto-rollbackable.",
        _write_properties(
            {"plan_id": {"type": "string", "format": "uuid"}}
        ),
        ["plan_id", "reason"],
        read_only=False,
    ),
    _tool(
        "import_organization_configuration",
        "Import configuration as approval plan",
        "Convert a portable SHVYA configuration export into an atomic approval plan for the active organization. Credentials are never imported; matching connected WhatsApp accounts must already exist.",
        {
            "configuration": {"type": "object"},
            "reason": {"type": "string", "minLength": 8, "maxLength": 500},
            "idempotency_key": {"type": "string", "maxLength": 128},
            "ttl_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
        },
        ["configuration", "reason"],
        read_only=False,
        requires_write_scope=True,
    ),
    _tool(
        "get_operations_audit",
        "Review SHVYA Operations audit",
        "Return safe organization-scoped audit events, or Superadmin-only platform audit events that have no customer tenant. Never mixes customer organizations.",
        {
            "scope": {
                "type": "string",
                "enum": ["organization", "platform"],
                "default": "organization",
                "description": (
                    "organization = active tenant only; platform = Superadmin-only "
                    "events with no organization."
                ),
            },
            "audit_event_id": {
                "type": "string",
                "format": "uuid",
            },
            "tool_name": {
                "type": "string",
                "maxLength": 100,
            },
            "target_type": {
                "type": "string",
                "maxLength": 80,
            },
            "target_id": {
                "type": "string",
                "maxLength": 100,
            },
            "support_context_id": {
                "type": "string",
                "format": "uuid",
            },
            "outcome": {
                "type": "string",
                "enum": [
                    "success",
                    "error",
                    "denied",
                    "approval_required",
                    "dry_run",
                ],
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 30,
            },
        },
    ),
]

DIAGNOSTIC_DEFINITIONS = []
for definition in DIAGNOSTIC_TOOL_DEFINITIONS:
    if definition["name"] not in DIAGNOSTIC_TOOL_NAMES:
        continue
    item = deepcopy(definition)
    item["securitySchemes"] = OAUTH_READ_SCHEMES
    item["_meta"] = {
        "securitySchemes": deepcopy(OAUTH_READ_SCHEMES),
    }
    DIAGNOSTIC_DEFINITIONS.append(item)

TOOL_DEFINITIONS = OWN_TOOL_DEFINITIONS + DIAGNOSTIC_DEFINITIONS
KNOWN_TOOLS = {item["name"] for item in TOOL_DEFINITIONS}
TOOL_INPUT_SCHEMAS = {
    item["name"]: item.get("inputSchema") or {"type": "object"}
    for item in TOOL_DEFINITIONS
}

TOOL_CAPABILITIES = {
    "get_operations_context": None,
    "list_organizations": None,
    "select_organization_context": None,
    "clear_organization_context": None,
    "get_organization_configuration": CAP_ORGANIZATION_READ,
    "get_ai_configuration": CAP_ORGANIZATION_READ,
    "get_knowledge_health": CAP_ORGANIZATION_READ,
    "get_automation_configuration": CAP_ORGANIZATION_READ,
    "get_messaging_automation_settings": CAP_ORGANIZATION_READ,
    "update_messaging_automation_settings": CAP_MESSAGING_CONFIG_WRITE,
    "diagnose_lead_qualification": CAP_DIAGNOSTICS_READ,
    "get_conversion_analysis": CAP_DIAGNOSTICS_READ,
    "move_lead_stage": CAP_LEAD_STAGE_WRITE,
    "repair_qualification_stage": CAP_LEAD_STAGE_WRITE,
    "update_lead_attributes": CAP_LEAD_ATTRIBUTES_WRITE,
    "update_ai_configuration": CAP_AI_CONFIG_WRITE,
    "upsert_pipeline_configuration": CAP_PIPELINE_CONFIG_WRITE,
    "upsert_stage_configuration": CAP_STAGE_CONFIG_WRITE,
    "upsert_attribute_configuration": CAP_ATTRIBUTE_CONFIG_WRITE,
    "upsert_workflow_configuration": CAP_WORKFLOW_CONFIG_WRITE,
    "upsert_cadence_configuration": CAP_CADENCE_CONFIG_WRITE,
    "add_cadence_step": CAP_CADENCE_CONFIG_WRITE,
    "get_qualification_configuration": CAP_ORGANIZATION_READ,
    "validate_qualification_configuration": CAP_ORGANIZATION_READ,
    "upsert_qualification_configuration": CAP_AI_CONFIG_WRITE,
    "list_whatsapp_accounts": CAP_ORGANIZATION_READ,
    "validate_whatsapp_routing": CAP_ORGANIZATION_READ,
    "bind_whatsapp_account_to_pipeline": CAP_MESSAGING_CONFIG_WRITE,
    "begin_whatsapp_connection": CAP_MESSAGING_CONFIG_WRITE,
    "list_workflow_triggers": CAP_ORGANIZATION_READ,
    "list_workflow_actions": CAP_ORGANIZATION_READ,
    "get_workflow_schema": CAP_ORGANIZATION_READ,
    "validate_workflow_configuration": CAP_ORGANIZATION_READ,
    "list_touchpoints": CAP_ORGANIZATION_READ,
    "upsert_touchpoint": CAP_CADENCE_CONFIG_WRITE,
    "archive_touchpoint": CAP_CADENCE_CONFIG_WRITE,
    "list_faqs": CAP_ORGANIZATION_READ,
    "upsert_faq": CAP_AI_CONFIG_WRITE,
    "archive_faq": CAP_AI_CONFIG_WRITE,
    "create_knowledge_source": CAP_AI_CONFIG_WRITE,
    "upload_knowledge_document": CAP_AI_CONFIG_WRITE,
    "publish_knowledge_document": CAP_AI_CONFIG_WRITE,
    "archive_knowledge_document": CAP_AI_CONFIG_WRITE,
    "add_hosted_whatsapp_step": CAP_CADENCE_CONFIG_WRITE,
    "update_cadence_step": CAP_CADENCE_CONFIG_WRITE,
    "delete_cadence_step": CAP_CADENCE_CONFIG_WRITE,
    "reorder_cadence_steps": CAP_CADENCE_CONFIG_WRITE,
    "simulate_ai_conversation": CAP_DIAGNOSTICS_READ,
    "simulate_workflow": CAP_DIAGNOSTICS_READ,
    "simulate_cadence": CAP_DIAGNOSTICS_READ,
    "get_configuration_dependency_graph": CAP_ORGANIZATION_READ,
    "validate_organization_configuration": CAP_ORGANIZATION_READ,
    "reorder_stages": CAP_STAGE_CONFIG_WRITE,
    "export_organization_configuration": CAP_ORGANIZATION_READ,
    "create_configuration_plan": CAP_CONFIGURATION_PLAN_WRITE,
    "apply_configuration_plan": CAP_CONFIGURATION_PLAN_WRITE,
    "rollback_configuration_plan": CAP_CONFIGURATION_PLAN_WRITE,
    "import_organization_configuration": CAP_CONFIGURATION_PLAN_WRITE,
    "get_operations_audit": CAP_AUDIT_READ,
}
for _diagnostic_name in DIAGNOSTIC_TOOL_NAMES:
    TOOL_CAPABILITIES[_diagnostic_name] = CAP_DIAGNOSTICS_READ


def _tool_requires_write_scope(item):
    schemes = item.get("securitySchemes") or []
    return any(
        OPERATIONS_WRITE_SCOPE in set(scheme.get("scopes") or [])
        for scheme in schemes
        if isinstance(scheme, dict)
    )


def _tools_for_identity(identity):
    """Return only tools this live policy + consent-bound OAuth grant can invoke."""

    has_write_scope = OPERATIONS_WRITE_SCOPE in identity.scopes
    granted = set(identity.granted_capabilities)

    if identity.role == ROLE_SUPERADMIN:
        visible = []
        for item in TOOL_DEFINITIONS:
            if _tool_requires_write_scope(item) and not has_write_scope:
                continue
            capability = TOOL_CAPABILITIES.get(item["name"])
            if capability is None or capability in granted:
                visible.append(item)
        return visible

    organization = identity.organization
    live_capabilities = set(
        effective_capabilities(
            role=identity.role,
            organization=organization,
        )
    )
    effective = live_capabilities & granted
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
        if _tool_requires_write_scope(item) and not has_write_scope:
            continue
        capability = TOOL_CAPABILITIES.get(name)
        if capability is None or capability in effective:
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
            "bearer_methods_supported": ["header"],
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


@ratelimit(limit=30, window=60)
@require_http_methods(["GET", "POST"])
def operations_oauth_authorize(request):
    if request.method == "POST" and _oauth_request_too_large(request):
        return _oauth_too_large_response()
    fields = _authorization_fields(request)
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
            safe_data = sanitize_data(
                execution.data,
                text_limit=TOOL_RESPONSE_TEXT_LIMITS.get(
                    tool_name,
                    800,
                ),
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
