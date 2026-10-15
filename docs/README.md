# SHVYA AI documentation map

> **Documentation refresh:** 2026-10-13, checked against `main` commit `bac4ebf2fcb1`. Code, migrations, tests, generated provider/tool discovery and deployment workflows remain authoritative when a document and implementation disagree.

SHVYA keeps both **current-reference documentation** and **historical implementation evidence**. Do not rewrite an incident/completion record to make it look current; instead use the current-reference guide for present behavior and the historical file for the implementation/recovery context it captured.

## Start here

| Need | Canonical document |
| --- | --- |
| Product/repository overview | [`../README.md`](../README.md) |
| Engineering rules | [`../CLAUDE.md`](../CLAUDE.md) |
| Database architecture | [`../database.md`](../database.md) |
| Local development | [`local_setup.md`](./local_setup.md) |
| Production release/deployment | [`deployment.md`](./deployment.md), [`deployment_environments.md`](./deployment_environments.md) |
| API/webhooks/MCP boundaries | [`api.md`](./api.md) |
| API operational history | [`api-operations.md`](./api-operations.md) |
| End-to-end architecture | [`system-architecture/README.md`](./system-architecture/README.md) |
| Operations MCP | [`operations-mcp.md`](./operations-mcp.md), [`operations-mcp-setup.md`](./operations-mcp-setup.md), [`operations-chatgpt-app.md`](./operations-chatgpt-app.md) |
| AI Brain / Playbook / grounding | [`ai-playbook.md`](./ai-playbook.md), [`organization-ai-brain-bundle.md`](./organization-ai-brain-bundle.md), [`grounded-engagement.md`](./grounded-engagement.md) |
| WhatsApp / Instagram | [`whatsapp_ai_troubleshooting.md`](./whatsapp_ai_troubleshooting.md), [`instagram-setup.md`](./instagram-setup.md) |
| SHVYA Sales | [`shvya-sales.md`](./shvya-sales.md) |
| SHVYA Calendar | [`shvya-calendar-workspace.md`](./shvya-calendar-workspace.md) |
| Call Intelligence / Android | [`call-intelligence.md`](./call-intelligence.md), [`android-release-publication.md`](./android-release-publication.md) |
| Vault | [`shvya-vault.md`](./shvya-vault.md) |
| Support | [`support-portal.md`](./support-portal.md), [`support-response-indicator.md`](./support-response-indicator.md) |
| Workflows / Cadence | [`smart-triggers.md`](./smart-triggers.md), [`workflows-runtime-contract.md`](./workflows-runtime-contract.md) |
| Scale/performance | [`scalability/production-scaling.md`](./scalability/production-scaling.md), [`performance/README.md`](./performance/README.md) |

## Website publishing

- [`website-blog.md`](./website-blog.md) — Blog Manager, draft revisions, scheduled publishing, images and search metadata.

## Connect Hub provider guides

- [`integrations/justdial.md`](./integrations/justdial.md) — JustDial JSON/GET lead ingestion, `SUCCESS` acknowledgement and replay handling.
- [`integrations/indiamart.md`](./integrations/indiamart.md) — IndiaMART Push API setup and receipt behavior.
- [`integrations/99acres.md`](./integrations/99acres.md) — 99acres native Push/Pull XML integration, encrypted per-organization credentials and sync behavior.
- [`meta-conversions-api.md`](./meta-conversions-api.md) — Meta Conversions API.
- Public customer documentation is rendered from `apps/core/docs_articles/*.json` at `/docs/` and should be updated with customer-visible behavior changes.

## Historical implementation evidence

Files named with terms such as `incident`, `completion`, `recovery`, `repair`, `validation`, `parity`, or a numbered phase can intentionally describe a specific implementation checkpoint. Examples include AI recovery/completion reports, Hosted WhatsApp repair notes and Instagram OAuth incident notes. Treat their dates/commit references as evidence of that checkpoint, not as the current release baseline.

Current-reference docs should point to those historical records when useful, but should not inherit their old branch/runtime assumptions.

## Documentation maintenance rules

1. Verify routes, model names, queues, capabilities and release versions in code before editing docs.
2. Keep `README.md`, `CLAUDE.md`, `api.md`, `architecture.md`, Operations MCP docs and `system-architecture/` synchronized when the external tool/skill surface changes.
3. Keep `/docs/` customer content tenant-neutral and do not expose internal IDs, credentials, private URLs, prompts, hidden scores or operational secrets.
4. A provider configuration or saved credential is not proof of live delivery. Document the receipt/read-back/acceptance evidence required for the claim.
5. Preserve historical incident/recovery documents as historical evidence instead of silently rewriting their original outcome.
6. The current release workflow is `main`-based. `docker-compose.staging.yml` is an optional isolated stack definition, not proof of an active staging-branch promotion path.
7. Authenticated MCP `tools/list`, `prompts/list` and `resources/list` are authoritative for the connected actor; repository counts describe the maximum catalog before capability filtering.
