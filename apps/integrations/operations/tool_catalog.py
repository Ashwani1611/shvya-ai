"""Operations MCP tool schemas, capability mapping, and visibility policy."""

from copy import deepcopy

from apps.integrations.operations.setup_catalog import (
    SETUP_TOOL_CAPABILITIES,
    setup_tool_definitions,
)
from apps.integrations.operations.extended_catalog import (
    EXTENDED_TOOL_CAPABILITIES,
    extended_tool_definitions,
)
from apps.integrations.operations_auth import (
    OFFLINE_SCOPE,
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
)
from apps.integrations.operations_policy import (
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
    ROLE_SUPERADMIN,
    effective_capabilities,
)
from apps.integrations.operations_tools import DIAGNOSTIC_TOOL_NAMES
from apps.integrations.views.mcp import TOOL_DEFINITIONS as DIAGNOSTIC_TOOL_DEFINITIONS


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
        "Dry-run or create/update a tenant-owned Cadence using SHVYA's canonical follow-up service. Always create with data.is_active=true; keep unfinished Cadences isolated from enrollment. Existing Cadence sender/provider cannot be changed; create a new Cadence for a different sender/provider.",
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
                        "is_active": {
                            "type": "boolean",
                            "description": "Set true for new Cadences (also the backend create default). Omit on updates to preserve the current status; set false only for an intended disable/archive.",
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
                        "attachments": {
                            "type": "array",
                            "maxItems": 5,
                            "description": "Email-only attachments. Combined decoded size may be up to 18 MiB.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "maxLength": 255},
                                    "mime_type": {"type": "string", "maxLength": 120},
                                    "content_base64": {"type": "string"},
                                },
                                "required": ["name", "content_base64"],
                                "additionalProperties": False,
                            },
                        },
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
        "list_whatsapp_templates",
        "List WhatsApp templates",
        "List organization-scoped WhatsApp templates and their current SHVYA/Meta status without exposing provider credentials.",
        {
            "whatsapp_account_id": {"type": "string", "format": "uuid"},
            "status": {
                "type": "string",
                "enum": [
                    "draft",
                    "pending",
                    "approved",
                    "rejected",
                    "paused",
                    "archived",
                    "pending_deletion",
                ],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        },
    ),
    _tool(
        "get_whatsapp_template_status",
        "Get WhatsApp template status",
        "Return one organization-scoped WhatsApp template, its local lifecycle state, Meta template ID/status, and rejection/error details.",
        {
            "template_id": {"type": "string", "format": "uuid"},
        },
        ["template_id"],
    ),
    _tool(
        "create_whatsapp_template",
        "Create WhatsApp template",
        "Dry-run or create a standard WhatsApp template draft for a connected Meta WABA using SHVYA's canonical template validation. This does not bypass Meta approval; call submit_whatsapp_template after creation.",
        _write_properties(
            {
                "whatsapp_account_id": {"type": "string", "format": "uuid"},
                "pipeline_id": {"type": "string", "format": "uuid"},
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 150,
                    "description": "Lowercase Meta template name using letters, numbers, and underscores.",
                },
                "body": {"type": "string", "minLength": 1, "maxLength": 4096},
                "category": {
                    "type": "string",
                    "enum": ["marketing", "utility", "authentication"],
                    "default": "marketing",
                },
                "language": {"type": "string", "maxLength": 20, "default": "en_US"},
                "footer": {"type": "string", "maxLength": 60},
                "buttons": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "object"},
                    "default": [],
                },
            }
        ),
        ["whatsapp_account_id", "name", "body", "reason"],
        read_only=False,
    ),
    _tool(
        "submit_whatsapp_template",
        "Submit WhatsApp template to Meta",
        "Dry-run or submit an existing WhatsApp template draft to the correct connected Meta WABA. Standard media templates are supported when a Meta header sample is already stored. Meta validation and approval remain authoritative.",
        _write_properties(
            {
                "template_id": {"type": "string", "format": "uuid"},
            }
        ),
        ["template_id", "reason"],
        read_only=False,
    ),
    _tool(
        "submit_whatsapp_templates",
        "Submit WhatsApp templates to Meta",
        "Dry-run or submit up to 50 organization WhatsApp template drafts to their correct connected Meta WABAs. Returns a per-template submitted, no-change, blocked, or failed result; Meta approval remains authoritative.",
        _write_properties(
            {
                "template_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {"type": "string", "format": "uuid"},
                },
            }
        ),
        ["template_ids", "reason"],
        read_only=False,
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
        "Dry-run or add a free-form Hosted WhatsApp message step using SHVYA's existing Hosted automation service. Optional base64 media is validated and stored through the canonical attachment rules up to the 50 MiB Hosted limit.",
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
                "data": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "maxLength": 255},
                        "body": {"type": "string"},
                        "subject": {"type": "string", "maxLength": 255},
                        "text": {"type": "string"},
                        "template_id": {"type": "string", "format": "uuid"},
                        "is_active": {"type": "boolean"},
                        "schedule": {"type": "object"},
                        "attachment_name": {"type": "string", "maxLength": 255},
                        "attachment_mime_type": {"type": "string", "maxLength": 120},
                        "attachment_base64": {"type": "string"},
                        "remove_attachment": {"type": "boolean"},
                        "attachments": {
                            "type": "array",
                            "maxItems": 5,
                            "description": "Email-only replacement attachments. Combined decoded size may be up to 18 MiB.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "maxLength": 255},
                                    "mime_type": {"type": "string", "maxLength": 120},
                                    "content_base64": {"type": "string"},
                                },
                                "required": ["name", "content_base64"],
                                "additionalProperties": False,
                            },
                        },
                        "remove_attachments": {"type": "boolean"},
                    },
                },
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
        "archive_stage",
        "Archive CRM stage",
        "Dry-run or archive one non-protected stage after dependency analysis. Lead, Workflow, qualification and integration dependencies must be migrated first.",
        _write_properties({"stage_id": {"type": "string", "format": "uuid"}}),
        ["stage_id", "reason"],
        read_only=False,
    ),
    _tool(
        "delete_stage",
        "Delete CRM stage",
        "Dry-run or permanently delete one non-protected dependency-free stage. Archiving is preferred; permanent deletion is irreversible.",
        _write_properties({"stage_id": {"type": "string", "format": "uuid"}}),
        ["stage_id", "reason"],
        read_only=False,
        destructive=True,
    ),
    _tool(
        "archive_attribute",
        "Archive CRM attribute",
        "Dry-run or archive one CRM attribute while preserving historical lead values. Active qualification, Workflow, stage requirement and integration mappings must be migrated first.",
        _write_properties({"attribute_id": {"type": "string", "format": "uuid"}}),
        ["attribute_id", "reason"],
        read_only=False,
    ),
    _tool(
        "delete_attribute",
        "Delete CRM attribute",
        "Dry-run or permanently delete one dependency-free CRM attribute. Lead values are preserved unless purge_values=true; purges are bounded and irreversible.",
        _write_properties(
            {
                "attribute_id": {"type": "string", "format": "uuid"},
                "purge_values": {"type": "boolean", "default": False},
            }
        ),
        ["attribute_id", "reason"],
        read_only=False,
        destructive=True,
    ),
    _tool(
        "archive_pipeline",
        "Archive CRM pipeline",
        "Dry-run or archive one dependency-free pipeline. Leads, Workflow references, WhatsApp routing and integration references must be migrated first.",
        _write_properties({"pipeline_id": {"type": "string", "format": "uuid"}}),
        ["pipeline_id", "reason"],
        read_only=False,
    ),
    _tool(
        "archive_cadence",
        "Archive Cadence",
        "Dry-run or archive one Cadence after active lead-state and Workflow dependency checks.",
        _write_properties({"cadence_id": {"type": "string", "format": "uuid"}}),
        ["cadence_id", "reason"],
        read_only=False,
    ),
    _tool(
        "archive_workflow",
        "Archive Workflow",
        "Dry-run or archive one Workflow. Archiving disables the rule and safely skips its pending/queued runs while retaining historical runs.",
        _write_properties({"workflow_id": {"type": "string", "format": "uuid"}}),
        ["workflow_id", "reason"],
        read_only=False,
    ),
    _tool(
        "test_integration_connection",
        "Test integration readiness",
        "Run safe tenant-scoped readiness checks for email, WhatsApp, Instagram, Google Sheets or outbound webhook. Email/Instagram may perform provider read/auth tests when live=true; no customer messages or webhook payloads are sent.",
        {
            "integration": {
                "type": "string",
                "enum": ["email", "whatsapp", "instagram", "google_sheets", "webhook"],
            },
            "resource_id": {"type": "string"},
            "live": {"type": "boolean", "default": False},
        },
        ["integration"],
    ),
    _tool(
        "compare_organization_configuration",
        "Compare organization configuration drift",
        "Superadmin-only comparison of the active organization with another active organization using opaque configuration fingerprints and counts only. Raw tenant configuration, customer records and credentials are never returned.",
        {
            "target_organization_id": {"type": "string", "format": "uuid"},
        },
        ["target_organization_id"],
    ),
    _tool(
        "get_configuration_integrity_diagnostics",
        "Find duplicate and orphaned configuration",
        "Detect duplicate configuration groups and orphaned/inactive references across CRM, Workflows and Cadence without changing state.",
    ),
    _tool(
        "test_ai_response_policy",
        "Test AI response policy",
        "Compile the current AI Playbook/engagement policy, surface configuration-policy issues, and optionally run the existing no-side-effect qualification simulation against synthetic answers.",
        {
            "answers": {"type": "object"},
        },
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

OWN_TOOL_DEFINITIONS.extend(setup_tool_definitions(_tool, _write_properties))
OWN_TOOL_DEFINITIONS.extend(extended_tool_definitions(_tool, _write_properties))

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
    "list_whatsapp_templates": CAP_ORGANIZATION_READ,
    "get_whatsapp_template_status": CAP_ORGANIZATION_READ,
    "create_whatsapp_template": CAP_MESSAGING_CONFIG_WRITE,
    "submit_whatsapp_template": CAP_MESSAGING_CONFIG_WRITE,
    "submit_whatsapp_templates": CAP_MESSAGING_CONFIG_WRITE,
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
    "archive_stage": CAP_STAGE_CONFIG_WRITE,
    "delete_stage": CAP_STAGE_CONFIG_WRITE,
    "archive_attribute": CAP_ATTRIBUTE_CONFIG_WRITE,
    "delete_attribute": CAP_ATTRIBUTE_CONFIG_WRITE,
    "archive_pipeline": CAP_PIPELINE_CONFIG_WRITE,
    "archive_cadence": CAP_CADENCE_CONFIG_WRITE,
    "archive_workflow": CAP_WORKFLOW_CONFIG_WRITE,
    "test_integration_connection": CAP_DIAGNOSTICS_READ,
    "compare_organization_configuration": CAP_DIAGNOSTICS_READ,
    "get_configuration_integrity_diagnostics": CAP_DIAGNOSTICS_READ,
    "test_ai_response_policy": CAP_DIAGNOSTICS_READ,
    "get_operations_audit": CAP_AUDIT_READ,
}
TOOL_CAPABILITIES.update(EXTENDED_TOOL_CAPABILITIES)
TOOL_CAPABILITIES.update(SETUP_TOOL_CAPABILITIES)
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
