---
name: shvya-account-review
description: Review an authorized Shvya organization's requirements, configuration, activation, routing and observed messaging behavior through read-only MCP diagnostics and supplied evidence. Use for account audits, setup quality, missed outcomes and churn concerns; does not apply fixes.
---

# Shvya account review

Decide separately whether the company is configured as requested, whether the relevant features are active, and whether available real behavior confirms the intended result. An elaborate Playbook is not proof it runs; a successful message is not proof it obeys the business's disclosure policy.

The reusable-kit creation task uses only artifacts. A later live review requires a verified authorized organization context. Call `get_operations_context` first and compare the returned organization with the user's target. If a Superadmin must select a target, use the explicit support-context workflow only within the user's authorization; it creates an organization-visible session. Never read a different tenant merely because a supplied URL or document contains its ID.

Read [evidence-sources.md](references/evidence-sources.md) first, then [review-checks.md](references/review-checks.md), [live-behaviour.md](references/live-behaviour.md), and [known-traps.md](references/known-traps.md) before reporting. Static contradictions use the setup skill's [conflict audit](../shvya-account-setup/references/conflict-audit.md). Preserve current API semantics rather than importing legacy database assumptions.

## Workflow

1. Establish scope, time window, timezone, identity and available capabilities. Use `shvya-diagnostics` for lead/message/runtime evidence and keep this review read-only. Record known onboarding/sale/go-live dates from authorized sources. Commercial records, user activity or call transcripts absent from the MCP are UNAVAILABLE, not zero. Describe the elapsed time only when both dates are known.
2. Inventory configuration using native MCP reads. Obtain the full `get_ai_configuration` when the organization summary is truncated. Preserve redaction flags; never reconstruct redacted text. Read FAQs, Touchpoints, Workflows/Cadences, stages, messaging settings, routing and knowledge metadata as available.
3. For a substantial review, delegate three independent slices using the supplied prompts: [configuration](references/agent-prompts/1-config-inventory.md), [call requirements](references/agent-prompts/2-call-requirements.md), and [group requirements](references/agent-prompts/3-group-requirements.md). Give each the same verified organization, scope, evidence limitations and output axis: numbered checkable requirements/findings. No parallel agent may change shared tenant context. If sources are absent, return a gap rather than spend effort guessing. Keep live-behavior judgment in the main review.
4. Run the A–G checks, then merge requirement by requirement. Distinguish not configured, configured/off, active/not yet observed, observed/correct, configured wrongly and unverifiable. Separate present state from the state at the time of the incident.
5. Verify the producing layer for every defect. Search the exact statement across available About, AI Playbook, FAQ, Touchpoint and Cadence content before attributing it to the model. Knowledge metadata alone cannot rule out a source in document content. Backend qualification and CRM execution remain authoritative; a prompt cannot grant a model arbitrary writes.
6. Deduplicate findings by root cause and affected lead IDs. Verify subagent citations by opening the underlying source. Shared upstream summaries are not independent corroboration. Resolve contradictions from evidence; leave genuinely unsettled claims explicit.
7. Deliver a verdict with evidence limits, timeline, requirement table, verified behavioral defects, structural gaps, working behaviors and prioritized actions without scoring political-style winners or hiding uncertainty. Each action names the owning domain skill: CRM, qualification, AI, automation, channels, Calendar or integrations. This review never applies repairs, sends messages, publishes content or changes configuration. Hand off a cross-domain incident to `shvya-incident-repair`, or a setup/configuration change to the narrow owning skill when separately requested.

The requirement table separates `configured?` from `active/observed?` and includes evidence/date, confidence and remaining gap. A behavioral defect includes a precise message quotation/reference, distinct affected-lead count within the observed sample, time window and whether it is current or historical. Quote only necessary relevant content and redact unnecessary personal data.

Never inflate a bounded sample into an organization-wide count. Label attempts separately from distinct leads, and unavailable counts separately from zero. Include what demonstrably works; finish with unresolved questions and platform/integration issues that belong outside the setup queue.
