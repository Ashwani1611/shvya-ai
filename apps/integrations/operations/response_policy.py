"""Tool-specific output bounds and redaction, independent of MCP transport."""

from apps.integrations.diagnostic_auth import sanitize_data, sanitize_text

from .setup_catalog import SETUP_TOOL_CAPABILITIES
from .setup_protocol import SETUP_LIBRARY_TOOL_NAMES


TOOL_RESPONSE_TEXT_LIMITS = {
    "get_automation_configuration": 20000,
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


# These readers expose nested canonical definitions, not raw provider payloads.
# The global diagnostics defaults remain unchanged.
CONFIGURATION_DETAIL_TOOLS = frozenset({
    "get_automation_configuration",
    "get_channel_cadence_configuration",
})


def _sanitization_changes(before, after, *, text_limit):
    """Return bounded paths/kinds only; never copy removed values into metadata."""
    paths = []
    count = 0
    truncated = False
    redacted = False

    def record(path, kind, omitted_count=None):
        nonlocal count, truncated, redacted
        count += 1
        truncated = truncated or kind in {"depth", "list", "text", "omitted"}
        redacted = redacted or kind == "redacted"
        if len(paths) < 100:
            item = {
                "path": sanitize_text(path or "/", limit=512),
                "kind": kind,
            }
            if omitted_count is not None:
                item["omitted_count"] = omitted_count
            paths.append(item)

    def walk(original, safe, path=""):
        if original == safe:
            return
        if safe == "[TRUNCATED]":
            record(path, "depth")
            return
        if safe == "[REDACTED]":
            record(path, "redacted")
            return
        if isinstance(original, dict) and isinstance(safe, dict):
            for key, value in original.items():
                key_text = str(key)
                pointer = key_text.replace("~", "~0").replace("/", "~1")
                child_path = path + "/" + pointer
                if key_text not in safe:
                    record(child_path, "omitted")
                else:
                    walk(value, safe[key_text], child_path)
            return
        if isinstance(original, (list, tuple)) and isinstance(safe, list):
            if len(original) > len(safe):
                record(path, "list", len(original) - len(safe))
            for index, value in enumerate(safe):
                walk(original[index], value, path + "/" + str(index))
            return
        if isinstance(original, str) and isinstance(safe, str):
            # Re-run only scalar redaction without the text bound so mixed
            # redaction/truncation is accurately reported as both kinds.
            unbounded = sanitize_text(original, limit=max(len(original) + 32, 800))
            if unbounded != original:
                record(path, "redacted")
            if len(unbounded) > text_limit:
                record(path, "text")
            return
        record(path, "omitted")

    walk(before, after)
    return {
        "changed": count > 0,
        "truncated": truncated,
        "redacted": redacted,
        "paths": paths,
        "change_count": count,
        "paths_truncated": count > len(paths),
    }


def sanitize_tool_response(tool_name, data):
    """Preserve reviewed library assets while bounding and redacting tenant data."""
    if tool_name in SETUP_LIBRARY_TOOL_NAMES:
        # Immutable, allowlisted repository assets contain no tenant data.
        # Generic credential heuristics would corrupt their documented
        # identifiers and example placeholders.
        return data
    if tool_name == "render_account_handover":
        # The dedicated renderer validates every input string with sanitize_text,
        # rejects credential-like content, escapes generated HTML, and returns
        # bounded artifact chunks with hashes. Re-sanitizing those generated
        # artifacts would corrupt HTML and invalidate their verified hashes.
        return data

    text_limit = TOOL_RESPONSE_TEXT_LIMITS.get(tool_name, 800)
    detailed = tool_name in CONFIGURATION_DETAIL_TOOLS
    safe_data = sanitize_data(
        data,
        text_limit=text_limit,
        list_limit=500 if tool_name == "analyze_setup_group_export" else 100,
        max_depth=12 if detailed else 6,
    )
    if detailed and isinstance(safe_data, dict):
        changes = _sanitization_changes(data, safe_data, text_limit=text_limit)
        semantic_redactions = (
            (data.get("counts") or {}).get("sensitive_workflow_fields_redacted", 0)
            if isinstance(data, dict) else 0
        )
        safe_data["response_sanitized"] = changes["changed"] or bool(semantic_redactions)
        safe_data["response_truncated"] = changes["truncated"]
        safe_data["response_redacted"] = changes["redacted"] or bool(semantic_redactions)
        safe_data["response_sanitization"] = {
            **changes,
            "semantic_workflow_fields_redacted": semantic_redactions,
        }
    elif tool_name in SETUP_TOOL_CAPABILITIES and safe_data != data:
        safe_data["response_sanitized"] = True
    return safe_data
