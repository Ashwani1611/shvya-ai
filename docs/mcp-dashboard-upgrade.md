# SHVYA MCP dashboard operations and full skills

This upgrade extends the existing Operations MCP with native dashboard operations and six complete workflows. The live `tools/list` catalog, effective tenant policy and OAuth grant remain authoritative. It does not make a connector an unrestricted database or Superadmin session.

## Terminology and authoring

| Kraya terminology | SHVYA surface |
| --- | --- |
| Quick replies / touchpoints | Touchpoints |
| AI setup / AI brain | AI Brain |
| Qualification instructions | AI Brain's canonical AI Playbook and qualification contract |
| Sequences | Cadences |
| Rules | Workflows |
| Client materials portal | Native SHVYA Vault in Superadmin |

The Playbooks area contains AI Brain, FAQs and knowledge documents. Behavioral policy belongs in the AI Playbook; company facts belong in About, grounded FAQs and approved knowledge. A file sharing instruction controls when a file is eligible for sharing. It is not a substitute for the AI Playbook.

Customer copy uses plain text and the supported first-name placeholder `{{lead_first_name}}`. API template variables require explicit validated delivery bindings. Authoring variables such as `{{SHVYA_*}}` must be resolved before publication. Industry examples are drafting guidance, never evidence of a client's prices, promises, contacts or policies.

## Native tools added

| Domain | Tools | Result and boundary |
| --- | --- | --- |
| Account provisioning | `create_organization_account` | Superadmin creates a native organization with default CRM initialization and an optional inactive owner. No credential is returned; secure password setup and owner activation remain explicit. |
| CRM | `list_crm_leads`, `get_crm_lead`, `create_crm_lead`, `update_crm_lead`, `import_crm_leads`, `bulk_move_crm_leads` | Read, create, import, update and move tenant-owned leads through canonical validation. Create/import use stable request IDs, reviewed duplicate handling and explicit row results. Welcome messages and Workflows are suppressed by default. |
| Channel discovery and readback | `get_channel_authoring_schema`, `get_channel_cadence_configuration` | Inspect exact sender modes, usable placeholders and full typed Cadence steps with pagination. |
| Cadences and templates | `upsert_channel_cadence`, `add_channel_cadence_step`, `configure_whatsapp_template_delivery` | API/Coexistence approved templates, Hosted free text and native Instagram steps. Cadences default active without enrolling leads or enabling triggers. |
| Support groups | `list_hosted_whatsapp_groups`, `read_hosted_whatsapp_group`, `send_hosted_whatsapp_group_message` | Exact tenant Hosted session and group ID. Sends require the selected member's owned sender, name prefix, exact-message approval and gateway idempotency. No edit/delete route. |
| Native Vault | `get_vault`, `export_vault`, `get_vault_entry`, `get_vault_asset`, `create_vault_workspace`, `upsert_vault_entry`, `upsert_vault_question`, `upsert_vault_call`, `upload_vault_asset`, `set_vault_section` | Real portal records, 15 sections, client overrides, source attribution, calls/questions and encrypted assets. Vault changes do not publish AI knowledge or send messages. |
| Knowledge | `list_playbook_documents`, `get_playbook_document`, `create_playbook_document`, `update_knowledge_document`, `repair_knowledge_document` | Versioned text documents, extracted chunks, indexing status, guided sharing and durable canonical ingestion repair. Saved/requested is distinguished from dispatched/completed. |
| Stateful conversation tests | `create_ai_flow_test_run`, `run_ai_flow_test_turn`, `get_ai_flow_test_run`, `cleanup_ai_flow_test_run` | Real disposable ORM fixtures through the production TurnController, answer persistence, CRM actions and summaries, with delivery blocked and approved provider budgets. |

Existing tools remain available for pipeline/stage/attribute configuration (including descriptions), full AI Playbook editing, FAQs, Touchpoints, Workflows, qualification, templates, calendar, diagnostics, configuration plans and readback. FAQ and Touchpoint reads now have explicit pagination so a review cannot silently stop at the first page. Knowledge uploads preserve sharing instructions and report asynchronous processing honestly.

## Six complete workflows

| User-facing workflow | Backend library prompt | Purpose |
| --- | --- | --- |
| `shvya-account-setup` | `shvya-account-setup` | Evidence intake, industry design, complete content/AI/CRM/channel authoring, dependency-ordered changes and verification. |
| `account-review` | `shvya-account-review` | Compare requirements, actual configuration and production evidence; separate findings from proposed fixes. |
| `ai-flow-testing` | `shvya-ai-flow-testing` | Design scenarios, create owned fixtures, run bounded turns, verify saved state and clean up. |
| `account-handover` | `shvya-account-handover` | Explain the verified journey and produce a practical onboarding demonstration guide. |
| `shvya-vault` | `shvya-vault` | Read and record client materials, questions, facts and call information with provenance. |
| `read-whatsapp-group` | `shvya-read-whatsapp-group` | Read support evidence and send only individually confirmed messages. |

The adaptation retains full specialist authoring prompts, industry guidance, review references, examples, helper-source provenance and behavioral evaluation rubrics. Original Kraya files are retained separately with hashes; they are historical source material, not executable SHVYA instructions. The operational guides resolve legacy contradictions about handoff, required-answer refusals, recipient tokens, fixed sequence counts and unavailable REST routes.

Use `prompts/list`, `prompts/get`, `resources/list`, `resources/read`, or the setup-library tools. Read all chunks of a selected specialist reference using `next_offset`; a truncated excerpt is not the whole skill. Behavioral eval files are explicitly unexecuted rubrics, not claims of measured model quality.

## Permissions and execution

New explicit capabilities are `organization.create`, `lead.read`, `lead.create`, `lead.write`, `lead.import`, `vault.read`, `vault.write`, `channel.group.read`, `channel.group.send` and `ai.flow_testing.write`. Tenant policies cannot grant platform organization creation. Existing tokens do not silently acquire these permissions: enable the relevant tenant controls and perform fresh OAuth authorization.

Writes retain actor/tenant/tool/proposal-bound dry-run receipts, drift checks, audit and native business validation. Hosted group sends and billable flow tests always require their own approval, even if ordinary edits are allowed without approval. They cannot be hidden inside a configuration plan. Credentials, Vault access codes and private signed asset URLs are not returned.

Creating a lead can notify configured CRM webhooks through canonical application behavior; suppressing welcome messages and Workflows is not a promise of zero integration events. Calendar-linked `booked_at` changes use the explicit Calendar tools rather than generic lead writes.

## What a flow test proves

The harness persists a server-owned, unroutable test lead and an inactive credential-free WhatsApp account. Default queries hide fixtures, a run-specific context exposes only its own records, and durable marker guards prevent production signals, Workflow dispatch and transport. Test CRM actions are limited to existing attributes, stage moves and owned notes. They cannot create real account schema, book calendar events or send external messages.

The run records configuration, idempotency, turns, usage and results. Provider calls use conservative credit reservations and bounded call/turn budgets. The MCP transport permits each turn's short claim/reservation transactions to commit before provider I/O. A worker interruption therefore leaves a durable running claim rather than silently permitting a billed replay. Inspect an interrupted run before recovery; do not clear or retry an ambiguous claim automatically.

This tests shared WhatsApp AI/CRM behavior. It does not validate webhooks, provider delivery, messaging windows, Workflows, Cadence scheduling, calendar bookings or Instagram's native inbound adapter. Deterministic simulations, mocked-provider automated tests, billable isolated flow tests and live channel evidence are reported separately. Cleanup only accepts the run ID and deletes its owned fixtures, preserving run results and usage audit.

## Deployment and activation

1. Deploy the application with the native Vault dependency and the new CRM, Channels and Integrations migrations; deploy the updated Hosted WhatsApp gateway for group endpoints.
2. Run migrations before serving traffic that queries the new fixture marker columns.
3. Verify the current `tools/list` and capability-discovery responses after deployment.
4. Enable only the required tenant permissions and reconnect/re-authorize the OAuth grant to include them.
5. Load the full workflow, review a concrete dry-run and apply approved changes. Verify asynchronous ingestion with document readback and actual delivery through channel evidence when authorized.

No customer account was configured, no real group message was sent, and no paid AI conversation was run as part of developing this upgrade. Automated tests replace network/provider calls with controlled doubles; a deployed smoke test is a separate step.

## Validation evidence

The completed local run passed **651 Django tests and 192 subtests**, covering the full Integrations and native Vault suites plus CRM integrity/filtering, CRM action execution, qualification end-to-end behavior, AI credit accounting, provider resilience, welcome handling and Instagram qualification actions. The five mocked Hosted gateway group tests also passed. Python lint, catalog/schema/handler consistency and migration-state checks passed.

Tests used an isolated PostgreSQL-compatible PGlite database with vector support and controlled provider doubles. They did not exercise production credentials, live channel delivery or a paid AI provider. The packaged library exposes 27 domain skills, 36 prompt entries and 265 text resources. Six personal workflows passed skill validation, and all 40 archived source files passed byte-length and SHA-256 verification.
