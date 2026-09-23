# Shvya diagnostics instead of legacy trace credentials

The source package used a company-specific LangSmith project, credentials, trace IDs and retention assumptions. None is Shvya configuration. Do not query that project, request its token or copy its URLs. Use Shvya's authorized tenant-scoped diagnostics first.

## Evidence path

1. Confirm active organization and requested incident/time window with `get_operations_context`.
2. Locate only the relevant lead using `find_leads` and its current schema; inspect `get_lead_snapshot` and `get_conversation` as needed. Prefer exact known IDs/phone, disambiguate results and avoid unrelated histories.
3. Use `trace_message({message_id})` for message processing/AI/hosted/workflow state, `get_workflow_trace` for workflow activity, `diagnose_lead_qualification` for qualification evidence/completion, and `get_ai_diagnostics` for AI execution health.
4. Check `get_recent_errors`, `get_runtime_health`, `get_integration_health`, `get_knowledge_health`, `get_configuration_integrity_diagnostics` and `validate_whatsapp_routing` only as relevant.
5. Compare the evidence against the saved configuration and [conflict audit](conflict-audit.md). State observed cause versus plausible explanation separately.

| Symptom | First checks |
|---|---|
| Repeated/missing question | Stable requirement IDs, active branch eligibility, persisted/current evidence, question parser output, flow version |
| Premature or missing Qualified transition | Required eligible answers, criteria, mappings, completion target, stage constraints, completion execution diagnostics |
| Wrong language or unsupported price | Full Playbook Rules/About, allowed languages, disclosure rules and accessible FAQ/source content |
| Brochure not sent | Is there a supported send action and real asset, or only retrieval indexing? Trace send state without claiming unavailable raw prompt access |
| No reply | Account/organization/pipeline/stage/lead AI switches, connection health, processing failures, business policy and opt-out |
| Duplicate follow-up | Cadence state, duplicate/overlapping Workflows, bump-ups, queued sends and replay outcomes |
| Wrong sender | Pipeline-bound account mapping and routing validation; never substitute another connected account |
| Uncertain delivery | Existing audit/delivery status; do not automatically resend a potentially delivered message |

`find_affected_leads` can locate bounded cohorts for a known persisted issue signal, but a cohort match is not permission to bulk repair. Diagnose each candidate before any authorized repair. `repair_qualification_stage`, `move_lead_stage` and `update_lead_attributes` have separate write capabilities and approval flows; they are not ordinary setup verification.

Return only the minimum relevant evidence: organization/lead/message or run ID, time, observed status, relevant config locator, likely cause, proposed fix and validation result. Do not request or expose hidden model reasoning, raw credentials, broad message histories or unrelated tenant data. If a safe diagnostic does not expose the needed prompt/retrieval trace, report that limit and give engineering a bounded investigation request.
