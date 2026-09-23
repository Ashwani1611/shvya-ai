# Shvya setup library in Operations MCP

This backend-owned library supplies five reusable workflows, eight setup/review subprompts, an operator prompt, references, typed Shvya authoring variables, generic and Ria examples, and evaluation rubrics. It is served through the existing authenticated Operations MCP, with the same actor, tenant, effective Allowed capabilities and bounded audit boundary. There is no second MCP server, client installer, shell tool or arbitrary filesystem reader.

## Start and retrieve progressively

1. Verify `get_operations_context`, the intended organization and effective capabilities.
2. Discover the current `tools/list` and `prompts/list` catalogs. Load `shvya-operator` or one task-specific prompt; do not load the entire library into every customer prompt.
3. Use `list_setup_library` for filtered resource discovery and `get_setup_library_resource(resource_id, offset, limit)` for bounded text. Native `resources/list` and `resources/read` expose the same immutable assets at `shvya-kit:///` URIs. Respect `truncated` and `next_offset` and retrieve remaining chunks before making a whole-document claim.
4. Read the task's linked references only as needed. All paths are immutable packaged IDs, not arbitrary server paths. Resource examples and operator-supplied prompt arguments never select a tenant or grant permissions.

## Workflows and backend actions

| Workflow | Native path |
|---|---|
| Account setup | Profile, canonical AI Playbook, About, grounded FAQs, Cadences, Workflows, CRM and validation through existing Operations tools |
| Account review | Evidence inventory, requirements comparison, configuration and observed-behavior review; proposes repairs without applying them |
| Intake vault | `get_setup_intake`, `upsert_setup_intake_entry`, `archive_setup_intake_entry`; tenant-scoped revisioned evidence with normal write controls |
| Group requirements | `analyze_setup_group_export`; validates a user-supplied normalized export for the active organization, returns bounded evidence and coverage |
| Voice preparation | `render_setup_template` produces policy and call-flow drafts; no provider provisioning, calls or transcript retrieval |

## Author company content

Read `templates/variables.md` and `get_setup_variable_schema`. Choose one of four template IDs: `ai-playbook`, `company-about`, `voice-agent`, `voice-call-instructions`. Supply all used values explicitly to `render_setup_template`; no Ria, company, contact or ID defaults are applied. All 41 Shvya variables are compile-time authoring values, not new CRM fields or environment secrets. Native runtime personalization remains separate.

Read `templates/generic-example.values.json` for fictional minimal authoring input and `templates/shvya-example.values.json` with `prompts/shvya-ria-ai-playbook.md` for the supplied Ria reference. Examples require business review and actual tenant-owned identifiers before use. The Ria contacts and four questions are not defaults for other companies.

The AI template uses the eight canonical sections of `OrgInfo.ai_playbook`. Rendering invokes the actual Playbook validator and question compiler, rejects structural injection and unresolved values, and produces a draft. Saving still requires `update_ai_configuration` with current tenant state, a specific reason, dry-run, required approval and read-back. Store factual business descriptions in About and approved factual knowledge in knowledge sources; do not put operating instructions in knowledge articles.

## Verification and limits

Evaluation JSON files are scenario rubrics, not claims of executed model or provider tests. Use the real backend validators and deterministic simulations to validate configuration, then separately authorized representative conversation tests for observed behavior. Record exactly which checks ran and what remains unavailable. Voice templates are drafts; group-export analysis does not retrieve live groups. Read `docs/backend-integration.md` for the adaptation and security contract.
