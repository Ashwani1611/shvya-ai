"""Schemas for the second-generation Operations capabilities."""

from apps.integrations.operations_policy import (
    CAP_CALENDAR_CONFIG_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_INTEGRATION_LIFECYCLE_WRITE,
    CAP_OPERATIONS_TASK_WRITE,
    CAP_ORGANIZATION_READ,
    CAP_TEAM_SETTINGS_WRITE,
)


def extended_tool_definitions(tool, write_properties):
    w = write_properties
    return [
        tool("get_capability_discovery", "Discover SHVYA capabilities", "Return live tools, permissions, dependencies, unsupported features and environment limitations."),
        tool("get_production_trace", "Inspect production AI trace", "Return bounded tenant-scoped trace metadata and, with trace.content.read, redacted prompt/input/output, attribution and run references.", {"trace_id": {"type": "string", "format": "uuid"}, "include_content": {"type": "boolean", "default": False}}, ["trace_id"]),
        tool("get_calendar_configuration", "Inspect Calendar configuration", "Return Calendar pages, booking rules, reminders and provider metadata without secrets."),
        tool("validate_calendar_configuration", "Validate Calendar configuration", "Check published pages, slot rules, reminder coverage and provider token presence."),
        tool("create_calendar_page", "Create a draft Calendar booking page", "Dry-run or create an unpublished Calendar page for the selected organization; further configuration and publishing are separate.", w({"name": {"type": "string", "minLength": 1, "maxLength": 120}, "slug": {"type": "string", "maxLength": 120}, "page_type": {"type": "string", "enum": ["lead", "booking", "lead_booking"], "default": "booking"}}), ["name", "reason"], read_only=False),
        tool("upsert_calendar_configuration", "Configure Calendar page", "Dry-run or update safe Calendar page configuration.", w({"page_id": {"type": "string", "format": "uuid"}, "changes": {"type": "object"}}), ["changes", "reason"], read_only=False),
        tool("verify_booking", "Verify booking", "Return booking status, provider sync and reminder delivery state.", {"booking_id": {"type": "string", "format": "uuid"}}, ["booking_id"]),
        tool("update_booking_status", "Update booking outcome", "Dry-run or mark a booking cancelled, completed or no-show; cancellation suppresses pending reminders.", w({"booking_id": {"type": "string", "format": "uuid"}, "status": {"type": "string", "enum": ["cancelled", "completed", "no_show"]}}), ["booking_id", "status", "reason"], read_only=False),
        tool("reschedule_booking", "Reschedule booking", "Dry-run or reschedule through canonical availability and Google sync services.", w({"booking_id": {"type": "string", "format": "uuid"}, "slot_start": {"type": "string"}}), ["booking_id", "slot_start", "reason"], read_only=False),
        tool("prepare_account_onboarding", "Prepare account onboarding orchestration", "Readiness check across AI Brain, CRM, qualification, automation, knowledge, integrations, Calendar and acceptance; no writes or activation."),
        tool("list_industry_playbooks", "List industry playbook templates", "Return tenant-independent stages, attributes, FAQs, sequences, rules and Touchpoints.", {"industry": {"type": "string", "maxLength": 80}}),
        tool("get_integration_lifecycle", "Inspect integration lifecycle", "Return current tenant-scoped provider state and supported lifecycle actions without credentials."),
        tool("disconnect_integration", "Safely disconnect integration", "Dry-run or disable one integration, clear provider credentials, preserve history and verify readback.", w({"integration": {"type": "string", "enum": ["whatsapp", "email", "google_calendar", "google_sheets", "webhook", "instagram"]}, "resource_id": {"type": "string", "format": "uuid"}}), ["integration", "resource_id", "reason"], read_only=False, destructive=True),
        tool("validate_cadence_batch", "Validate bulk Cadence changes", "Validate ordering, overlap, recipient variable binding and suppression without sends or enrollment.", {"cadence_id": {"type": "string", "format": "uuid"}, "steps": {"type": "array", "items": {"type": "object"}}}),
        tool("run_acceptance_suite", "Run no-send acceptance suite", "Exercise qualification, multilingual, refusal, pricing, handoff, opt-out, Cadence, Workflow and delivery readiness without provider calls."),
        tool("list_commitments", "List operational commitments", "Return follow-up commitments from onboarding, integrations, audits and acceptance blockers.", {"status": {"type": "string", "enum": ["open", "in_progress", "blocked", "completed", "cancelled"]}}),
        tool("upsert_commitment", "Track operational commitment", "Dry-run or create/update an operational task record; it cannot send or activate automation.", w({"commitment_id": {"type": "string", "format": "uuid"}, "data": {"type": "object"}}), ["data", "reason"], read_only=False),
        tool("get_team_settings", "Inspect team settings", "Return responder hours, AI ownership, handoff, sender identity and Co-Pilot state without secrets."),
        tool("upsert_team_settings", "Configure team settings", "Dry-run or update responder hours, AI ownership, handoff, sender identity and Co-Pilot behavior; no messages are sent and automation is not activated.", w({"changes": {"type": "object"}}), ["changes", "reason"], read_only=False),
    ]


EXTENDED_TOOL_CAPABILITIES = {
    "get_capability_discovery": None,
    "get_production_trace": CAP_DIAGNOSTICS_READ,
    "get_calendar_configuration": CAP_ORGANIZATION_READ,
    "validate_calendar_configuration": CAP_ORGANIZATION_READ,
    "create_calendar_page": CAP_CALENDAR_CONFIG_WRITE,
    "upsert_calendar_configuration": CAP_CALENDAR_CONFIG_WRITE,
    "verify_booking": CAP_ORGANIZATION_READ,
    "update_booking_status": CAP_CALENDAR_CONFIG_WRITE,
    "reschedule_booking": CAP_CALENDAR_CONFIG_WRITE,
    "prepare_account_onboarding": CAP_ORGANIZATION_READ,
    "list_industry_playbooks": CAP_ORGANIZATION_READ,
    "get_integration_lifecycle": CAP_ORGANIZATION_READ,
    "disconnect_integration": CAP_INTEGRATION_LIFECYCLE_WRITE,
    "validate_cadence_batch": CAP_ORGANIZATION_READ,
    "run_acceptance_suite": CAP_ORGANIZATION_READ,
    "list_commitments": CAP_ORGANIZATION_READ,
    "upsert_commitment": CAP_OPERATIONS_TASK_WRITE,
    "get_team_settings": CAP_ORGANIZATION_READ,
    "upsert_team_settings": CAP_TEAM_SETTINGS_WRITE,
}
