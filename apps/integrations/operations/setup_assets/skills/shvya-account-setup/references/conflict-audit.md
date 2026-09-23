# Configuration conflict and grounding audit

Run before publishing a setup, after changing a repeated fact or promise, and when a reported answer seems invented. The purpose is to find preventable conflicts and unavailable material, not to claim every AI failure is configuration.

## Gather scoped evidence

Read the full `get_ai_configuration`, qualification configuration, current CRM definitions, `get_automation_configuration`, FAQs, Touchpoints, account settings and available knowledge metadata. Use authorized source files and approved website text for content-level comparison. Health metadata cannot establish the exact contents of a document; label uninspected material as unverified. Include both current and proposed versions with entity ID and section/message locator. Never substitute another tenant's configuration.

## Checks

**A. Fact ledger.** Extract contacts, links, addresses, prices/fees/discount terms, quantities/MOQs, service areas, hours, dates, inventory, credentials/claims, people, product names, eligibility and policies. Compare equivalent facts only within the same product/location/effective period. Normalize phone/currency/date/units without erasing distinctions. Different valid branch contacts are not automatically a contradiction. Flag near-identical numbers and stale versions.

**B. Promises and material.** Find every promise to answer/share/send/book/confirm/call. Match each to exact approved content and a supported mechanism. A RERA or other identifier needs its exact source value. A brochure needs a deliverable attachment/link plus allowed send path. A booking needs a confirmed system/human action. A payment claim needs verified state. An uploaded retrieval document is not automatically a sendable asset. If material is missing, supply it through authorized setup or replace the promise with a human handoff.

**C. Instruction agreement.** Compare Rules, questions, criteria, stage/mapping/reminder logic and preferences. Check required/optional disagreements, conflicting pricing disclosure, duplicate welcomes, contradictory persona, language mismatch, refusal versus qualification gates, impossible timing, stale question numbering, multiple completion targets and unsupported branch operators.

**D. Platform fit.** Match exact returned stages, attribute keys/options, active sender/pipeline, permitted capabilities and real tool fields. Inspect branch eligibility and question IDs. Check API templates are approved and owned by the selected sender. No unsupported multi-action Workflow, calendar offset, round-robin action or arbitrary placeholder may pass as implemented.

**E. Stale/seeded content.** Search for source-company identities, demo services, old offers, expired batch/event dates, sample URLs, draft labels, test leads and templated claims. Names alone do not prove a record is disposable; verify ownership and dependencies before requested retirement.

**F. Integration reality.** Every channel/source described as live must have a verified ingestion/delivery path. A health/auth check is not a full data-flow test. Record the gap and owner where actual end-to-end operation remains untested.

**G. Automation interlocks.** Check opt-out, human handoff, replies, terminal outcomes, no-response routing, recovery completion, re-entry and bump-ups together. Look for duplicate messages and rules that restart a stopped Cadence. Recovery ending in its own entry stage is a cycle. Distinguish Lost from Do Not Contact before considering revival.

**H. Follow-up questions primed by copy.** If a message advertises a price, event, feature, offer, file, support promise or payment option, the AI must have the approved answer to predictable follow-up questions or a truthful handoff. Remove copy that creates an unsupported expectation.

## Finding format

| Severity | Type | Entity/locator | Evidence | Consequence | Proposed resolution | Owner/status |
|---|---|---|---|---|---|---|
| Block publish | Missing required gate mapping | Playbook Q2 | Mapped key does not exist | Required evidence cannot persist reliably | Bind/create the approved attribute and revalidate | Pending |

Block affected publication for unresolved factual contradictions, unsupported claims/promises, unbound dependencies, opt-out/loop failures and invalid qualification. Other independent drafts can still be handed over. Resolve disputed facts with an authoritative company source or responsible owner; do not choose a convenient value. Update every affected representation and retain provenance.

## Verification and limitations

Compile stored qualification and policy, simulate valid/incomplete/conditional/refusal/correction cases, validate organization/routing and preview Cadence schedules. Workflow simulation requires a tenant lead and synthetic event; do not create a live customer test record without scope.

These tools do not generate model responses. They cannot prove multilingual copy quality, retrieval relevance or exact live provider delivery. Provide a separate authorized staging conversation test plan for those: direct factual questions, branch transitions, early pricing, handoff, refusal, STOP, attachment request, supported languages and changed facts. Report actual observed results; do not reuse the legacy demo endpoint or invent a pass percentage.

For a reported hallucination, preserve the observed message and IDs, inspect accessible diagnostics, locate source/instruction conflicts and possible old conversation contamination, then propose a narrow correction. Fixing configuration does not automatically repair prior summaries or sent messages; lead-specific repair is separate authorized work.
