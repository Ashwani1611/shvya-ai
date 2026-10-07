"""Explicit CRM and platform account tools for Operations MCP."""

from apps.integrations.operations_policy import (
    CAP_LEAD_CREATE,
    CAP_LEAD_IMPORT,
    CAP_LEAD_READ,
    CAP_LEAD_STAGE_WRITE,
    CAP_LEAD_WRITE,
    CAP_ORGANIZATION_CREATE,
)

LEAD_PROPERTIES = {
    "name": {"type": "string", "minLength": 1, "maxLength": 150},
    "phone": {"type": "string", "maxLength": 32, "description": "Country-code phone, e.g. +919876543210; empty only for Instagram leads."},
    "email": {"type": "string", "maxLength": 254},
    "notes": {"type": "string", "maxLength": 10000},
    "lead_source": {"type": "string", "enum": ["system", "external_api", "whatsapp_api", "whatsapp", "google_sheets", "indiamart", "csv_import", "meta_ads", "instagram", "shvya_calendar", "phone_call", "justdial"]},
    "ai_enabled": {"type": "boolean"},
    "auto_followup_enabled": {"type": "boolean"},
    "attributes": {"type": "object", "description": "Values keyed by existing, active, non-sensitive organization attribute keys. Internal AI state cannot be supplied. Use Calendar tools for booked_at to review booking/reminder side effects."},
}


def dashboard_crm_tool_definitions(tool, write_properties):
    uuid = {"type": "string", "format": "uuid"}
    data = {"type": "object", "properties": LEAD_PROPERTIES, "additionalProperties": False}
    create_data = {**data, "required": ["name", "phone"]}
    creation_controls = {"client_request_id": {**uuid, "description": "Caller-generated UUID. Reuse on retries; deterministic server lead IDs prevent duplicate creation, including phone-less Instagram leads."},
                         "allow_workflows": {"type": "boolean", "default": False, "description": "Explicitly allow lead-created Workflows. Default false prevents automatic workflow fan-out from this provisioning action."}}
    return [
        tool("list_crm_leads", "Find CRM leads", "Read tenant-scoped CRM contacts with bounded pagination and explicit lead.read consent.", {
            "query": {"type": "string", "maxLength": 200}, "pipeline_id": uuid, "stage_id": uuid,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0},
        }),
        tool("get_crm_lead", "Read CRM lead", "Read one CRM contact, active non-sensitive attributes and available attribute descriptions; excludes internal AI state.", {"lead_id": uuid}, ["lead_id"]),
        tool("create_crm_lead", "Create CRM lead", "Preview or create a validated lead through the canonical CRM service. Welcome and Workflow emission are suppressed by default. Defaults AI and follow-ups off.", write_properties({"pipeline_id": uuid, "stage_id": uuid, "data": create_data, **creation_controls}), ["pipeline_id", "stage_id", "data", "client_request_id", "reason"], read_only=False),
        tool("update_crm_lead", "Edit CRM contact", "Preview or update contact fields and existing non-sensitive attributes, preserving internal state and CRM history. Use move_lead_stage for routing changes.", write_properties({"lead_id": uuid, "changes": {**data, "minProperties": 1}}), ["lead_id", "changes", "reason"], read_only=False),
        tool("import_crm_leads", "Import CRM leads", "Preview or atomically import up to 100 structured rows per call. Existing phone numbers are skipped or rejected, never overwritten. Normalize CSV/spreadsheet rows first and use bounded batches. Welcome and Workflow emission are suppressed by default.", write_properties({
            "pipeline_id": uuid, "stage_id": uuid,
            "rows": {"type": "array", "minItems": 1, "maxItems": 100, "items": create_data},
            "duplicate_policy": {"type": "string", "enum": ["error", "skip"], "default": "error"},
            **creation_controls,
        }), ["pipeline_id", "stage_id", "rows", "client_request_id", "reason"], read_only=False),
        tool("bulk_move_crm_leads", "Move CRM lead batch", "Preview or atomically move 1–100 exact lead IDs. Rechecks tenant, qualification completion and required attributes for every lead; records canonical history and may trigger existing stage-moved Workflows.", write_properties({"lead_ids": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True, "items": uuid}, "target_stage_id": uuid}), ["lead_ids", "target_stage_id", "reason"], read_only=False),
        tool("create_organization_account", "Create organization account", "Superadmin only. Preview and create an organization using the same validated form and default CRM initialization as the Superadmin console. Optional owner is an inactive administrator with an unusable password; Superadmin must set password and activate through the console. No credentials, channel connections or outbound notifications are created. Explicitly select its context afterward.", write_properties({"data": {
            "type": "object", "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "package": {"type": "string", "enum": ["free", "diy", "dfy", "enterprise"], "default": "free"},
                "number_of_seats": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1},
                "owner": {"type": "object", "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 150},
                    "email": {"type": "string", "format": "email"},
                    "phone": {"type": "string", "maxLength": 32, "description": "Free accounts require a valid Indian mobile number, using canonical signup normalization."},
                }, "required": ["name", "email", "phone"], "additionalProperties": False},
            }, "required": ["name"], "additionalProperties": False,
        }}), ["data", "reason"], read_only=False),
    ]


DASHBOARD_CRM_TOOL_CAPABILITIES = {
    "list_crm_leads": CAP_LEAD_READ,
    "get_crm_lead": CAP_LEAD_READ,
    "create_crm_lead": CAP_LEAD_CREATE,
    "update_crm_lead": CAP_LEAD_WRITE,
    "import_crm_leads": CAP_LEAD_IMPORT,
    "bulk_move_crm_leads": CAP_LEAD_STAGE_WRITE,
    "create_organization_account": CAP_ORGANIZATION_CREATE,
}
