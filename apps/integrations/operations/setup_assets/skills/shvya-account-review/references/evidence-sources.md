# Evidence sources and access boundaries

Tool names below are suffixes of `mcp__shvya_ai_superadmin__`. The host may display a shorter name; use only a discovered callable schema and the effective Allowed capabilities. Discover exact currently exposed schemas with MCP `tools/list`; packaged examples are guidance, never permission to call unavailable tools.

| Evidence | Available MCP reads | What they cannot establish |
|---|---|---|
| Tenant/actor | `get_operations_context` | Authority over another organization |
| Configuration | `get_organization_configuration`, `get_ai_configuration`, `get_qualification_configuration`, `export_organization_configuration` | Historic runtime values unless separately audited; exports omit leads, history, binaries and credentials |
| FAQ/Touchpoint | `list_faqs`, `list_touchpoints` | That every stored item was actually served to a lead |
| Automation | `get_automation_configuration`, `get_workflow_schema`, `get_configuration_dependency_graph`, `get_configuration_integrity_diagnostics` | Population-wide execution counts if not returned; generic data fields do not establish supported actions |
| Routing/settings | `list_whatsapp_accounts`, `get_messaging_automation_settings`, `validate_whatsapp_routing`, `get_integration_health` | Hosted group history or provider secrets |
| Runtime summary | `get_runtime_health`, `get_conversion_analysis`, `get_recent_errors` | Causality from correlation; indefinite history or raw model traces |
| Leads | `find_leads`, `find_affected_leads`, `get_lead_snapshot` | A complete random cohort or unrestricted lead export; cohort reads are bounded |
| Lead behavior | `get_conversation`, `trace_message`, `get_workflow_trace`, `get_ai_diagnostics`, `diagnose_lead_qualification` | Raw provider webhooks, all model prompts, or voice calls; message media URLs are redacted |
| Knowledge | `get_knowledge_health` | Document text, retrieval contents, signed URLs, file bytes or vectors |
| Audit | `get_operations_audit` with organization scope | All historic non-MCP edits or unrelated customer events |
| Read-only verification | `validate_organization_configuration`, `test_ai_response_policy`, `simulate_ai_conversation`, `simulate_cadence`, `simulate_workflow` | Live delivery or actual model wording; simulations have narrower contracts |
| Business requirements | User-supplied calls, documents, intake vault and group exports | Missing history or uninspected media; no built-in Fireflies/Vault/group-read capability is assumed |
| Commercial/onboarding frame | Authorized supplied contracts, sale records and operational summaries | Subscription entitlements, credit ledger, seats, last login or CRM card fields not exposed by MCP |

For integration readiness checks, `test_integration_connection` defaults to local readiness where supported; `live=true` may perform external provider auth/read checks. It sends no customer messages, but live checks are unnecessary for an artifact-only review.

Before reading, record a scope manifest: organization ID/name, actor/capability summary, time window/timezone, source IDs and collection timestamps, tool limits/truncation/redaction, and which evidence was not available. Store each source separately for the same tenant. Do not paste raw credentials or other tenants' rows into a finding.

Requirements precedence is contextual: an explicit current authorized client correction about a specific scope can supersede an older requirement; otherwise preserve contradictory statements and ask which applies. Direct client words are stronger requirement evidence than a summary or relayed CRM note, while canonical backend data is stronger evidence of what actually executed. A customer complaint establishes a symptom, not its technical cause.

When a source is missing, identify what would settle it: e.g. an authorized call transcript, relevant knowledge document, message UUID or existing audit event. Do not search credential files, access production databases, use legacy REST login, download raw traces or infer connector installation. Native Shvya tools cover the supported path.
