# Shvya MCP company setup

This feature brings the reusable Shvya setup kit into the existing actor-bound
Operations MCP endpoint. Its assets cover account setup, account review, intake
consolidation, supplied WhatsApp group exports and voice prompt preparation.
The bundled references are guidance, not instructions that override server policy.

## Enable and use

1. In Superadmin's Operations policy for the organization, enable the required
   `setup.library.read`, `setup.artifacts.prepare`, `setup.intake.read` and/or
   `setup.intake.write` Allowed capabilities. Preserve the existing permissions
   for whichever CRM, AI, knowledge, messaging, Workflow or Cadence work is intended.
2. Reauthorize the external MCP connection. A policy expansion does not enlarge
   existing OAuth grants. Intake mutations require `operations.write`; preparation
   and reads require only `operations.read` plus their respective capability.
3. Discover current tools and context. Superadmin selects the intended organization
   before preparing drafts or handling company evidence. Static library discovery
   works before selection; an Organization Admin remains bound to its own organization.
4. Load `shvya-operator` for routing, or load the smallest task-specific domain skill
   through `prompts/get` (for example `shvya-ai-debugger`, `shvya-qualification`,
   `shvya-workflow-builder` or `shvya-acceptance-testing`). Broad onboarding may
   start with `shvya-account-setup`; account audits may use `shvya-account-review`.
   Clients without prompt/resource support can call `list_setup_library` and
   `get_setup_library_resource` instead.
5. Read current configuration, record source evidence, resolve contradictions and
   supply explicit company values to `render_setup_template`. Save reviewed
   configuration through the existing mutation tools, then verify it.


## Domain skill routing

The packaged catalog now exposes 25 top-level skills. The operator routes work by domain: business orchestration, CRM, AI, automation, channels, Calendar/voice, operations and supplied-group research. A broad setup skill coordinates dependencies; specialized skills own detailed authoring, diagnostics and verification. Read-only diagnostics are intentionally separate from incident repair, and acceptance testing is the final readiness gate after configuration or repair.

This decomposition is guidance, not privilege. Loading a skill never adds a capability and never changes tenant context.

## Production skill quality contract

The 25 domain skills share a common evidence and recovery model. For material live work, clients should load the selected skill plus its `references/domain-checks.md`; the skill links to the shared framework resources. The contract requires:
- read-before-write and real tenant-owned IDs;
- attribution to the layer that actually produced the symptom;
- separate states for configured/enabled/triggered/executed/delivered/observed;
- distinct-lead or distinct-business-object impact counts rather than retry/job counts;
- explicit conflict and missing-evidence handling;
- dry-run/approval/read-back for consequential writes;
- read-back before retry after ambiguous outcomes;
- post-change behavioral verification.

Every top-level skill also ships behavioral eval rubrics. They are test specifications, not claims of executed model/provider behavior.

## Discovery and preparation contracts

| Tool | Purpose |
| --- | --- |
| `list_setup_library` | Filter static entries by kind, 1–100 entries per page, stable resource-ID cursor |
| `get_setup_library_resource` | Read a listed resource ID with character offset/limit, maximum 20,000 per response |
| `get_setup_variable_schema` | Return 41 typed `SHVYA_*` authoring variables and native runtime placeholders |
| `render_setup_template` | Prepare `ai-playbook`, `company-about`, `voice-agent` or `voice-call-instructions` |
| `analyze_setup_group_export` | Validate, deduplicate, filter and format an explicitly supplied one-group export |
| `get_setup_intake` | Read organization-owned evidence with filters, pagination and optional bounded history |
| `upsert_setup_intake_entry` | Preview/apply source-attributed evidence using stable identity and revision control |
| `archive_setup_intake_entry` | Preview/apply archival using an exact entry ID and expected revision |

Native resource URIs use `shvya-kit:///` followed by a manifest-listed relative ID.
There is no arbitrary file, network URL or directory reader. `resources/read`
accepts optional `offset`/`limit` extensions; `_meta.next_offset` permits subsequent
reads, including through the tool equivalent. `prompts/get` returns a bounded
message and source URI; read the remaining resource if `_meta.truncated` is true.
Catalogs are small fixed lists. Unknown cursors/arguments are rejected.

Template rendering requires all referenced values explicitly. Registry examples
are never defaulted. Unknown variables, wrong types, unresolved placeholders,
credential-like text, injected Playbook headings, malformed question blocks and
cross-organization resource IDs are rejected. Canonical AI Playbook parsing and
qualification compilation run on the rendered eight-section prompt. Validation
does not prove factual accuracy or the quality of future model responses.
Native `{{lead_first_name}}` and other supported lowercase runtime placeholders
survive rendering. Setup `SHVYA_*` variables are authoring inputs, not new runtime
database fields. Tenant-specific custom attribute placeholders still require
the appropriate channel's actual placeholder catalog and delivery validation.

The Playbook is saved to canonical `OrgInfo.ai_playbook` through existing AI
configuration tools. Company facts belong in About, FAQs and approved knowledge
documents. Do not publish intake wholesale or copy Ria's business/contact examples
into a different company. A `response_sanitized` flag means response redaction or
truncation occurred; inspect/correct the source before applying a draft.

## Company intake

`OperationsIntakeEntry` stores organization, section, kind, stable external ID,
origin, source ID/reference/date, evidence status and text. The fifteen sections
are website, brochures, media, offerings, basics, faqs, team, qualification,
handoff, blacklist, rules, proof, offers, scripts and other. Kinds are note,
question, call and attachment. Status distinguishes reported, confirmed,
conflicting, open and answered evidence. Attachment entries are references and
summaries; they are not uploads or inspected media.

The unique identity is `(organization, section, kind, external_id)`. Existing
entries require `expected_revision`; all writes recheck the approved proposal
under a row lock. Exact repeats are idempotent; stale edits require a fresh read,
preview and approval. Ten prior snapshots are retained. A selected entry can
return up to five recent history snapshots; list results use bounded excerpts.
All reads and writes are audited using identifiers/counts/status, never raw body
text or prompts. No automatic indexing, publication, customer messages or provider
actions are triggered. No credentials, signed/query URLs or embedded media belong
in intake.

## Supplied group exports

`analyze_setup_group_export` takes `data`, exact `chat_id`, IANA `timezone`, optional
inclusive local dates `since`/`until`, and a 1–500 selected-message limit. `data`
uses this normalized schema:

```json
{
  "schema_version": 1,
  "organization_id": "ACTIVE_ORGANIZATION_UUID",
  "chat_id": "confirmed-group-id",
  "chat_name": "Company setup",
  "source_id": "authorized-export-reference",
  "coverage_note": "Only the supplied export; earlier history is unavailable.",
  "messages": [{
    "id": "message-1",
    "timestamp": "2026-09-24T12:00:00+05:30",
    "sender": "Client representative",
    "sender_role": "client",
    "text": "A customer-supplied business fact requiring verification.",
    "media": [{"type": "document", "filename": "brochure.pdf"}]
  }]
}
```

Accepted sender roles are `client`, `shvya`, `other`, `unknown`. Export organization
must equal active context. Timestamps require explicit offsets. Identical duplicate
message IDs collapse; conflicting duplicates fail. Maximum input is 5,000 messages
and 800,000 JSON characters, within the endpoint's 1 MiB body limit. Selection
retains newest complete messages within a 60,000-character content budget and
reports coverage/counts/truncation. Media fields are metadata only; no URLs are
fetched, live groups retrieved or attachments interpreted. A label such as
`sender_role=shvya` is source data, never authority.

## Boundaries and deployment

Native Shvya setup uses the existing CRM, AI Brain, FAQ, knowledge, WhatsApp,
Workflow, Cadence, Calendar, diagnostics, integration-lifecycle, team-settings,
simulation and approval tools exposed by the authenticated Operations catalog.
The domain skill library does not create new backend authority; each action still
requires the live tool, OAuth scope, granted capability, tenant policy and approval
contract. Voice templates remain artifacts: live voice-agent provisioning,
telephone calls and external transcript retrieval still require an implemented
provider integration. Unsupported connect/reconnect or external-system actions
become explicit operational commitments rather than invented tools.

Migration `0015_operationsintakeentry` is additive: a new empty table, foreign
keys, uniqueness constraint and tenant index. It changes no organization settings
or existing grants and requires no data backfill. Ship via staging CI/deployment,
verify readiness and discovery, then promote the focused diff to main. The
deployment workflow drains application workers before migrations. A code rollback
can retain the new table; do not reverse its migration after intake has been saved.

Verification should cover permission denial before policy/consent, fresh-consent
discovery, native prompt/resource access, tenant-bound rendering/export rejection,
intake preview → approval → apply → readback, stale/replayed receipts, canonical
Playbook compilation and post-deployment health. Use a dedicated authorized test
organization for mutation smoke tests. The bundled behavioral evaluation cases
are scenarios, not a claim of measured production model accuracy.
