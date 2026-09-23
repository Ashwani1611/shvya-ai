"""Shared constants for Operations MCP configuration management."""

from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
)

PLAN_TTL_MINUTES = 30
PLAN_MAX_TTL_MINUTES = 24 * 60
PLAN_MAX_OPERATIONS = 200
EXPORT_SCHEMA_VERSION = 1

PUBLIC_PLAN_TOOLS = {
    "update_ai_configuration",
    "upsert_qualification_configuration",
    "upsert_pipeline_configuration",
    "upsert_stage_configuration",
    "upsert_attribute_configuration",
    "upsert_workflow_configuration",
    "upsert_cadence_configuration",
    "add_cadence_step",
    "add_hosted_whatsapp_step",
    "update_cadence_step",
    "reorder_cadence_steps",
    "upsert_touchpoint",
    "archive_touchpoint",
    "upsert_faq",
    "archive_faq",
    "update_messaging_automation_settings",
    "reorder_stages",
}
INTERNAL_PLAN_TOOLS = {
    "_ensure_stage_configuration",
}
ALLOWED_PLAN_TOOLS = PUBLIC_PLAN_TOOLS | INTERNAL_PLAN_TOOLS

PLAN_TOOL_CAPABILITIES = {
    "update_ai_configuration": CAP_AI_CONFIG_WRITE,
    "upsert_qualification_configuration": CAP_AI_CONFIG_WRITE,
    "upsert_pipeline_configuration": CAP_PIPELINE_CONFIG_WRITE,
    "upsert_stage_configuration": CAP_STAGE_CONFIG_WRITE,
    "_ensure_stage_configuration": CAP_STAGE_CONFIG_WRITE,
    "upsert_attribute_configuration": CAP_ATTRIBUTE_CONFIG_WRITE,
    "upsert_workflow_configuration": CAP_WORKFLOW_CONFIG_WRITE,
    "upsert_cadence_configuration": CAP_CADENCE_CONFIG_WRITE,
    "add_cadence_step": CAP_CADENCE_CONFIG_WRITE,
    "add_hosted_whatsapp_step": CAP_CADENCE_CONFIG_WRITE,
    "update_cadence_step": CAP_CADENCE_CONFIG_WRITE,
    "reorder_cadence_steps": CAP_CADENCE_CONFIG_WRITE,
    "upsert_touchpoint": CAP_CADENCE_CONFIG_WRITE,
    "archive_touchpoint": CAP_CADENCE_CONFIG_WRITE,
    "upsert_faq": CAP_AI_CONFIG_WRITE,
    "archive_faq": CAP_AI_CONFIG_WRITE,
    "update_messaging_automation_settings": CAP_MESSAGING_CONFIG_WRITE,
    "reorder_stages": CAP_STAGE_CONFIG_WRITE,
}
