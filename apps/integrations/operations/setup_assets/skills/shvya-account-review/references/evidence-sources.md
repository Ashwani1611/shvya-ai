# Reference contents

- Evidence sources and access boundaries
- Complete review method from supplied reference
- Evidence sources for an account review
- What you can reach
- 1. Kraya REST API — what is configured
- 2. Production MySQL — what actually happened
- 3. LangSmith — what the bot actually said
- 4. Fireflies — what the client asked for
- 5. The client's WhatsApp support group — requirements, complaints, promises
- 6. Ops CRM — the frame
- Access gotchas that cost time

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


# Complete review method from supplied reference

> SHVYA ADAPTATION: Read runtime-contract.md before using this full reference. Preserve this method's detail, but compile its output into native SHVYA schemas. Kraya field names, API routes, database queries, token scripts and past performance claims are historical context, not live capabilities. Sample businesses, prices and policies remain examples. Human handoff/opt-out takes precedence over continued qualification; use verified double-brace CRM tokens and provider bindings. This reference does not authorize sending, enrollment or activation.

# Evidence sources for an account review

Six sources, each answering something the others cannot. What you can reach depends on where you are running.

## What you can reach

| Source | Cloud agent | Claude Code session with the Kraya MCPs |
|---|---|---|
| Kraya REST API as the client | yes (`POST /auth/login`) | yes |
| Production MySQL, read-only | **no** | yes (`mysql-prod` MCP) |
| LangSmith traces | yes (`LANGSMITH_API_KEY`) | yes |
| Client WhatsApp support group | yes (`read-whatsapp-group` skill) | yes |
| Fireflies call recordings | only if a Fireflies tool is present | usually yes |
| Ops CRM (Postgres) | **no** | yes, from the repo's `.env.local` |
| CloudWatch | **no** | yes (`claude-cw-kraya` profile) |

The cloud agent has no SuperAdmin and no database. It can still run checks A, D, E and most of F from the API and LangSmith alone; B and C get thinner, because activation counts (`rule_executions`, leads per stage, sequence assignments) are database facts. **Say in the report which checks ran at full depth and which did not.** A review that silently skipped activation is worse than one that says it could not measure it.

## 1. Kraya REST API — what is configured

The authoritative read of the account as the client sees it. Endpoints, envelopes and field meanings are in `kraya-account-setup/references/api-reference.md`; the gathering table in `conflict-audit.md` §1 already lists the exact call per config area, so use that list rather than rebuilding it.

Covers: org info (`about`, `qualification_requirements`, `bot_languages`, `attachments`, `sendable_files`), stages and their `ai_switch`, attributes, FAQs, sequences and their messages, rules, quick replies, bump-ups, templates, calendar settings.

Does not cover: whether any of it ever ran.

## 2. Production MySQL — what actually happened

Read-only. Validate every column against `Kraya-Laravel/.cursor/rules/database-schema.mdc` before querying; it is the source of truth for column names and types. Never write.

The rows that decide the review:

| Question | Where |
|---|---|
| Has any rule ever fired? | `rule_executions` filtered by org. **Zero here invalidates the whole automation layer** |
| Which sequences are live, and to whom? | `auto_responder_sequences` + per-sequence lead assignment counts |
| Is the funnel reachable? | `leads` grouped by `stage_id`. Stages with 0 leads mean their rules never fire |
| Where do leads come from? | `leads.source` breakdown, plus daily counts |
| Is the channel connected? | `waha_sessions` (hosted) and `whatsapp_accounts` (Cloud API) |
| What is being consumed? | `credit_logs` by type and by day; burn rate against the balance |
| Bookings | `leads.booked_at`, `calendar_configurations` |
| Health | latest `organization_health_scores` row |
| Integrations | the per-integration tables; report `NONE` explicitly, never by omission |

Traps: `leads` has **no `deleted_at`** column. `organizations.id` is a `char(36)` UUID, so never `chunkById` or `orderBy('id')` on it. More in `known-traps.md`.

## 3. LangSmith — what the bot actually said

The only place a hosted-WhatsApp or extension conversation exists. **Kraya does not persist those messages**: `whatsapp_messages` holds Cloud API webhook payloads only, so for most accounts the database has no transcripts at all and LangSmith is the sole record of what went to leads.

Access and run shape are in `kraya-account-setup/references/langsmith.md`. That file is written for investigating one lead; the population sweep across an entire org is in `live-behaviour.md`.

Each root run carries `organization_id`, `lead_id`, `template`, `request_mode` and `environment` in metadata, the rendered `org_info` and the full transcript in `inputs.params`, and the bot's decision in `outputs.output`. That means a single fetched run gives you the live org info, the stage list, the model in use and the conversation — useful when you have no database.

## 4. Fireflies — what the client asked for

The primary requirement source. Search by business name, by participant email, by the rep's and POC's names, and by a date-range scan over titles; clients are recorded under inconsistent names.

Expect a business assessment call shortly after the sale, an onboarding call after setup, and support calls when things break. **The sales call is frequently missing or untranscribed** — two consecutive reviews found the pre-sale promises existed nowhere in text. When that happens, say so: it means the commitments behind the purchase are unauditable, and that is a finding about the process, not just the account.

## 5. The client's WhatsApp support group — requirements, complaints, promises

Use the `read-whatsapp-group` skill. Often richer than the calls: it carries the corrections the client sent afterwards, the bugs they hit, what ops promised, and how long each side went quiet.

Read it for four things the calls do not give you: every configuration change requested after go-live, every complaint and whether it was resolved, every Kraya promise and whether it was visibly delivered, and the silence gaps on both sides. An `iterations_requested` flag on the card means the change requests are in here.

## 6. Ops CRM — the frame

Stage, time in stage, flags, POC, handover, sale, and the BAC notes or attributes holding the client's stated numbers (lead volume, AOV, sources, appointment vs direct sale). Postgres connection string is in the `Kraya-Ops-CRM` repo's `.env.local`.

The card is the weakest of the requirement sources: it is a relay of a relay. Use it for the frame and for flags, and take requirements from the client's own words.

## Access gotchas that cost time

- **Timezone.** The MySQL MCP renders timestamps through the host machine's local zone, so values can appear shifted by hours. Wrap in `CONVERT_TZ(...,'+00:00','+05:30')` for IST and, if the number still looks wrong, check the host's zone with `date` before concluding anything. Laravel logs in CloudWatch are UTC; build epoch windows with an explicit `timezone.utc`, never a naive local `datetime`.
- **Fireflies list metadata is unreliable** — `num_sentences` and `duration` frequently read `0` on list endpoints. Never conclude a call is empty from the list; fetch the transcript.
- **WhatsApp group history depth differs per ops member.** Each hosted session only sees messages since that phone joined the group. Try several and use the one with the earliest message; report which session and which start date, because an early gap may be invisible rather than absent.
- **Voice notes and screenshots cannot be read** through the group API. Clients often send corrections as voice notes. Note their timestamps and say plainly that some requests may be inside them.
- **Ops members without a hosted session** post from their own phones, so their messages arrive with `fromMe: false`. Identify sides by `senderName`, never by `fromMe`.
- **Use `curl`, not Python `urllib`**, for Kraya and WAHA endpoints from a local session; `urllib` fails SSL verification on machines without `certifi`.
