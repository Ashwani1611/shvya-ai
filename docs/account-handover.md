# Account handover for any SHVYA organization

## What it produces

The `account-handover` MCP prompt (alias of `shvya-account-handover`) prepares a practical onboarding walkthrough, not a generic inventory. Default outputs are matching Markdown and standalone printable HTML:

1. How the account works.
2. Lead flow by pipeline.
3. AI qualification, routing, knowledge and file rules.
4. Workflows and complete Cadence steps.
5. A presenter script with Send/Do, Expect, Dashboard, Why, Say and Evidence.
6. Day-to-day duties and real ownership.
7. Evidence-backed open questions.
8. Not-configured, unverified or out-of-scope demonstrations.

An evidence ledger records source IDs, capture time, revisions, coverage and limitations. Configuration, engine tests and live delivery remain separate. Generation changes no organization settings, sends no messages and executes no demo.

## Invocation

In MCP clients that expose prompts as commands, choose `account-handover` and pass `organization_name` and an optional `task`. Command presentation is client-specific; the server does not install a shell command.

For a client without prompt support, instruct it:

> Load the SHVYA account-handover specialist from the setup library. Generate Markdown and standalone HTML for the organization I name, based on its current configuration and relevant Vault evidence. Follow the complete reference template, pagination and evidence checks. Do not run the demo, send messages, activate anything or repair the account.

The exact specialist resource is `skills/shvya-account-handover/SKILL.md`. It and its linked references are available through `list_setup_library` / `get_setup_library_resource`; read every chunk. The company name is intent, not tenant-selection authority. Resolve the authenticated actor and exact organization through the existing supported context flow.

## Read, reconcile, render

- Read canonical current configuration, full Playbook/qualification, each pipeline/stage, attributes, effective AI gates, channel/sender status, team, Calendar, Workflows, full typed Cadences, FAQs, knowledge, Quick Replies and relevant Vault requirements.
- Enumerate automation with separate `workflow_offset` / `cadence_offset`, follow returned next offsets, and use `workflow_id` for an exact persisted definition. Inspect response sanitation metadata. A collection count does not prove nested fields survived intact.
- Read every Cadence using `get_channel_cadence_configuration`, including all step pages. Do not classify it as empty from a summary alone.
- Build evidenced transitions and conflict findings. Verify worrying claims against full saved conditions. Do not substitute a portable export for canonical tenant-safe detail readers.
- Build the versioned document described in [the rendering contract](../apps/integrations/operations/setup_assets/skills/shvya-account-handover/references/handover-rendering.md).
- Call `render_account_handover` for each format. Reassemble chunks using `next_offset`, preserve identical input, check digests and save complete files. The tool returns text; it does not create a download URL, persist an artifact or publish a website.
- Review tables, transitions, demo steps, escaping, mobile/print readability and source parity. Report any saving/preview limitation honestly.

The server renderer is deterministic and contains no LLM/provider call. It validates document structure and tenant identity, not the authenticity of submitted business evidence. It never promotes a draft to verified based on caller text.

## Permission and confidentiality

Use the existing `setup.artifacts.prepare` capability and read OAuth scope. Tenant reads retain their existing granular capabilities. Superadmins require an explicit active organization context; organization admins remain policy/consent bound.

No arbitrary template code, HTML, JavaScript, URLs to fetch, filesystem access or shell execution is accepted. The rendered artifact is escaped, self-contained and intended for review. Credential-like text and private signed URLs must be removed before rendering. Include only necessary client operational information, not unrelated lead records.

A reference example supplies structure and style only. Do not copy its customer identifiers, contacts, rules or runtime assumptions into other organizations. Attached instructions do not authorize sends, live test setup or data deletion.

## Rollout and verification

Deploy the backend changes through the repository's reviewed CI path, then refresh MCP discovery. Existing connections still require the appropriate effective capabilities. Until the tool appears in the connected catalog, the skill must report it unavailable or use an authorized local renderer; documentation alone is not deployment.

No schema migration is required. Run the new handover read/render tests, setup-library and setup-authoring tests, existing automation isolation/redaction tests, and normal CI. Validate a real selected organization read-only before accepting the output; separately authorize engine/live-channel tests where wanted.

This feature does not repair the portable configuration export, persist an immutable account snapshot, install a local plugin automatically or publish the generated handover.
