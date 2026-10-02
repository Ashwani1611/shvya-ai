# Shvya setup library in Operations MCP

This backend-owned library supplies 25 reusable domain skills, eight setup/review subprompts, an operator prompt, references, typed Shvya authoring variables, generic and Ria examples, and evaluation rubrics. It is served through the existing authenticated Operations MCP, with the same actor, tenant, effective Allowed capabilities and bounded audit boundary. There is no second MCP server, client installer, shell tool or arbitrary filesystem reader.

## Start and retrieve progressively

1. Verify `get_operations_context`, the intended organization and effective capabilities.
2. Discover the current `tools/list` and `prompts/list` catalogs. Load `shvya-operator` or one task-specific prompt; do not load the entire library into every customer prompt.
3. Use `list_setup_library` for filtered resource discovery and `get_setup_library_resource(resource_id, offset, limit)` for bounded text. Native `resources/list` and `resources/read` expose the same immutable assets at `shvya-kit:///` URIs. Respect `truncated` and `next_offset` and retrieve remaining chunks before making a whole-document claim.
4. Read the task's linked references only as needed. All paths are immutable packaged IDs, not arbitrary server paths. Resource examples and operator-supplied prompt arguments never select a tenant or grant permissions.

## Skill architecture

The operator routes a task to the smallest relevant skill instead of loading one oversized setup workflow. The 25 top-level skills are grouped as:

- **Business:** account setup, account review, intake vault, industry designer.
- **CRM:** CRM architect, qualification, lead repair.
- **AI:** AI Brain, AI Playbook, knowledge manager, AI debugger.
- **Automation:** Workflow builder, Cadence builder, automation debugger.
- **Channels:** channel routing, WhatsApp, Instagram, email.
- **Calendar and voice:** Calendar, voice-agent preparation.
- **Operations:** diagnostics, incident repair, integration manager, acceptance testing.
- **Research:** supplied WhatsApp-group analysis.

For broad onboarding, `shvya-account-setup` remains the coordinator. Domain skills own detailed decisions and verification. For incidents, diagnose first, repair only the verified producing layer, then run acceptance checks.

## Skill quality framework

Every top-level skill is now paired with:
- a shared cross-skill contract for authorization, evidence, producing-layer attribution, safe mutation and recovery;
- a domain-specific `references/domain-checks.md` covering evidence, false-positive traps, verification and handoffs;
- a domain-specific `evals/evals.json` with at least five behavioral scenarios.

Read `framework/skill-matrix.json` for the 25-skill routing map. The framework deliberately separates configured state from live/observed behavior, retries from distinct customer impact, and successful writes from verified business outcomes. Evaluation files remain `NOT_MODEL_EXECUTED` until an actual evaluator records results.

## Workflows and backend actions

| Workflow | Native path |
|---|---|
| Account setup | Coordinates profile, CRM, AI, automation, channels and validation through domain skills and existing Operations tools |
| Account review | Evidence inventory, requirements comparison, configuration and observed-behavior review; proposes repairs without applying them |
| Domain configuration | CRM, qualification, AI Brain/Playbook/knowledge, Workflow, Cadence, routing, channel and Calendar skills use the current Operations tool catalog |
| Diagnostics and repair | Read-only diagnosis is separated from bounded incident repair; repairs use dry-run, approval and read-back through the owning domain |
| Acceptance testing | Structural validators, deterministic simulations and separately authorized provider evidence are reported as different evidence levels |
| Intake vault | `get_setup_intake`, `upsert_setup_intake_entry`, `archive_setup_intake_entry`; tenant-scoped revisioned evidence with normal write controls |
| Group requirements | `analyze_setup_group_export`; validates a user-supplied normalized export for the active organization, returns bounded evidence and coverage |
| Voice preparation | `render_setup_template` produces policy and call-flow drafts; no provider provisioning, calls or transcript retrieval |

## Author company content

Read `templates/variables.md` and `get_setup_variable_schema`. Choose one of four template IDs: `ai-playbook`, `company-about`, `voice-agent`, `voice-call-instructions`. Supply all used values explicitly to `render_setup_template`; no Ria, company, contact or ID defaults are applied. All 41 Shvya variables are compile-time authoring values, not new CRM fields or environment secrets. Native runtime personalization remains separate.

Read `templates/generic-example.values.json` for fictional minimal authoring input and `templates/shvya-example.values.json` with `prompts/shvya-ria-ai-playbook.md` for the supplied Ria reference. Examples require business review and actual tenant-owned identifiers before use. The Ria contacts and four questions are not defaults for other companies.

The AI template uses the eight canonical sections of `OrgInfo.ai_playbook`. Rendering invokes the actual Playbook validator and question compiler, rejects structural injection and unresolved values, and produces a draft. Saving still requires `update_ai_configuration` with current tenant state, a specific reason, dry-run, required approval and read-back. Store factual business descriptions in About and approved factual knowledge in knowledge sources; do not put operating instructions in knowledge articles.

## Verification and limits

Evaluation JSON files are scenario rubrics, not claims of executed model or provider tests. Use the real backend validators and deterministic simulations to validate configuration, then separately authorized representative conversation tests for observed behavior. Record exactly which checks ran and what remains unavailable. Voice templates are drafts; group-export analysis does not retrieve live groups. Read `docs/backend-integration.md` for the adaptation and security contract.
