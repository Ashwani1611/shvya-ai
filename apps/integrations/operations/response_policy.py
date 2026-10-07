"""Tool-specific output bounds and redaction, independent of MCP transport."""

from apps.integrations.diagnostic_auth import sanitize_data

from .setup_catalog import SETUP_TOOL_CAPABILITIES
from .setup_protocol import SETUP_LIBRARY_TOOL_NAMES


TOOL_RESPONSE_TEXT_LIMITS = {
    "get_channel_cadence_configuration": 20000,
    "read_hosted_whatsapp_group": 10000,
    "send_hosted_whatsapp_group_message": 10500,
    "get_ai_flow_test_run": 20000,
    "run_ai_flow_test_turn": 20000,
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
    "get_vault": 20000,
    "export_vault": 20000,
    "get_vault_entry": 20000,
    "get_vault_asset": 20000,
    "create_vault_workspace": 20000,
    "upsert_vault_entry": 20000,
    "upsert_vault_question": 20000,
    "upsert_vault_call": 20000,
    "upload_vault_asset": 20000,
    "set_vault_section": 20000,
    "list_playbook_documents": 20000,
    "get_playbook_document": 20000,
    "create_playbook_document": 20000,
    "update_knowledge_document": 20000,
    "repair_knowledge_document": 20000,
    "list_faqs": 20000,
    "list_touchpoints": 20000,
    "get_crm_lead": 20000,
    "list_crm_leads": 20000,
}


def sanitize_tool_response(tool_name, data):
    """Preserve reviewed library assets while bounding and redacting tenant data."""
    if tool_name in SETUP_LIBRARY_TOOL_NAMES:
        # Immutable, allowlisted repository assets contain no tenant data.
        # Generic credential heuristics would corrupt their documented
        # identifiers and example placeholders.
        return data

    safe_data = sanitize_data(
        data,
        text_limit=TOOL_RESPONSE_TEXT_LIMITS.get(tool_name, 800),
        list_limit=500 if tool_name == "analyze_setup_group_export" else 100,
    )
    if tool_name in SETUP_TOOL_CAPABILITIES and safe_data != data:
        safe_data["response_sanitized"] = True
    return safe_data
