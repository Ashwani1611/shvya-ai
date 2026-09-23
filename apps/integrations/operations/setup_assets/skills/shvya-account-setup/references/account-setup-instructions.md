# Configurable Shvya areas

This is the operator map. Exact tool arguments come from the current MCP schema, not this table.

| Area | Shvya artifact | Authoring and verification requirement |
|---|---|---|
| Qualification | AI Brain > AI Playbook | Canonical questions, branch eligibility, evidence criteria, acknowledgment, attribute mappings, stage/reminder rules; compile and simulate |
| Company information | AI profile `about`, `bot_languages` | Stable verified facts, identity, service coverage and permitted languages; never make About a second conflicting Playbook |
| Knowledge | URL knowledge sources, documents/chunks/publication | Approved sources, versions and provenance; ready ingestion and publication before claiming retrieval works |
| FAQs | organization question/answer records | Grounded concise answers, deduped by question; current `upsert_faq` has no category fields |
| Workflows | event + conditions + one action | Canonical schema, real tenant IDs, Source filter, stops and loop prevention; validate/simulate |
| Attributes | text/numeric/date/datetime/option definitions | One described field per collected decision, exact returned keys and option values; avoid credentials and unnecessary sensitive data |
| Stages/pipelines | CRM funnel with AI flags | One meaning and owner per stage; preserve protected stages; no automatic payment/won assertion from chat text |
| Cadences | sender-bound sequence and ordered steps | Hosted free-form, API approved templates, email or internal reminders as actually supported; preview timing |
| Integrations | connected channel and source configuration | Inventory, mapping, consent, ownership and health; unsupported connection steps become tasks |
| Touchpoints | saved replies and categories | Real company-specific content in useful operator groups; one reply serves one situation |
| Messaging settings | account and organization switches | Pipeline-linked routing, business hours, active-conversation delay, AI/follow-up/bump-up settings; scope overlaps audited |
| Commitments | tracked implementation ledger | Verbatim promise, date, due date or none given, owner, status; no invented Ops CRM endpoint |

A requested calendar, team assignment, round robin, billing change, template creation or arbitrary integration is not automatically supported by setup tools. Check the currently exposed contract. If absent, deliver exact content/mapping and a task for the responsible owner. Do not represent a draft as connected, booked, paid, uploaded or delivered.

Suggested scale, never a quota: 6–12 described attributes, only meaningful stages, 3–6 useful Cadences, 15–30 grounded FAQs and 12–20 Touchpoints. Small funnels may need much less. Prefer a compact complete Playbook over a prescribed character target.
