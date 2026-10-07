"""Operations contracts for metered, isolated production AI/CRM tests."""
from apps.integrations.operations_policy import CAP_DIAGNOSTICS_READ

CAP_AI_FLOW_TEST_WRITE = "ai.flow_testing.write"
FLOW_TEST_TOOL_CAPABILITIES = {
    "create_ai_flow_test_run": CAP_AI_FLOW_TEST_WRITE,
    "run_ai_flow_test_turn": CAP_AI_FLOW_TEST_WRITE,
    "get_ai_flow_test_run": CAP_DIAGNOSTICS_READ,
    "cleanup_ai_flow_test_run": CAP_AI_FLOW_TEST_WRITE,
}


def flow_testing_tool_definitions(tool, write_properties):
    run_id = {"type": "string", "format": "uuid"}
    key = {"type": "string", "minLength": 1, "maxLength": 100}
    return [
        tool("create_ai_flow_test_run", "Create isolated production AI test run",
            "Create a server-owned unroutable real Lead and inactive credential-free WhatsApp fixture. Uses current AI Setup. No customer transport or configuration change. Subsequent turns are billable and capped; this does not test provider delivery, workflows, calendar, or native Instagram adapter.",
            {**write_properties(), "name": {"type": "string", "minLength": 1, "maxLength": 120},
             "pipeline_id": run_id, "stage_id": run_id, "idempotency_key": key,
             "channel": {"type": "string", "enum": ["whatsapp"], "default": "whatsapp"},
             "max_turns": {"type": "integer", "minimum": 1, "maximum": 500, "default": 30},
             "max_provider_calls": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 100},
             "max_credits": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 1000}},
            ["pipeline_id", "stage_id", "idempotency_key", "reason"], read_only=False),
        tool("run_ai_flow_test_turn", "Run billable isolated AI turn",
            "Run the production TurnController, answer persistence, CRM executor and internal summary on this run's disposable Lead. Inspect actual saved attributes/stage/summary. Enforces turn, provider-call and credit budgets under locks; idempotency prevents retries being billed twice. Never sends messages. Configuration drift requires a fresh run.",
            {**write_properties(), "run_id": run_id, "idempotency_key": key,
             "message": {"type": "string", "minLength": 1, "maxLength": 4000}},
            ["run_id", "idempotency_key", "message", "reason"], read_only=False),
        tool("get_ai_flow_test_run", "Inspect isolated AI run and persisted state",
            "Read tenant-owned run results, actual saved CRM state, summary, per-run metered credits and limitations. Does not call the AI provider or test delivery.",
            {"run_id": run_id, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}, ["run_id"]),
        tool("cleanup_ai_flow_test_run", "Remove owned AI test fixtures",
            "Delete only the server-manifest-owned disposable Lead and inactive test account, including their dependent test records. Caller-supplied lead/account IDs are prohibited. Rejects cleanup while a turn is running. Keeps run results, usage accounting and audit evidence. No organization settings need restoration.",
            {**write_properties(), "run_id": run_id}, ["run_id", "reason"], read_only=False),
    ]
