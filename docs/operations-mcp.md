# SHVYA Operations MCP

> **Implementation baseline:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. This documentation commit is docs-only; runtime code, migrations, tests, and deployment configuration remain the executable source of truth.

> This document describes the actor-bound Operations MCP implemented on the staging branch. The existing Diagnostic MCP remains a separate read-only connector.

## Purpose

SHVYA Operations MCP lets an authorized external AI client such as ChatGPT, Claude, or VS Code inspect, diagnose, propose and, where explicitly permitted, apply bounded SHVYA changes.

The backend remains authoritative. The external AI never grants itself a role, tenant, capability or approval.

## Endpoints

- MCP resource: `/operations/mcp/`
- OAuth registration: `/operations/oauth/register`
- OAuth authorization: `/operations/oauth/authorize`
- OAuth token: `/operations/oauth/token`
- OAuth revocation: `/operations/oauth/revoke`
- Protected-resource metadata: `/operations/.well-known/oauth-protected-resource`
- Authorization-server metadata: `/.well-known/oauth-authorization-server/operations`

Operations OAuth uses PKCE S256. Access and refresh values are stored only as hashes. Revoking an Operations OAuth grant immediately invalidates its access/refresh grant and closes any active Superadmin support session tied to it; the revocation action itself is safely audited. Public client redirect URIs are restricted to HTTPS ChatGPT/OpenAI and Claude/Anthropic hosts plus the exact VS Code MCP callbacks `http://127.0.0.1:33418` and `https://vscode.dev/redirect`. Arbitrary localhost ports/hosts and arbitrary `vscode.dev` paths are rejected.

## VS Code connection

VS Code can connect to the remote Streamable HTTP endpoint at `/operations/mcp/` and use the same actor-bound OAuth flow. For MCP 2026-07-28 clients, SHVYA advertises Client ID Metadata Documents (CIMD) as the preferred public-client registration path and keeps Dynamic Client Registration as a backward-compatible fallback. The exact VS Code MCP redirect URLs remain enforced.

A typical VS Code MCP configuration points an HTTP server entry at the deployed `https://<host>/operations/mcp/` URL. Authentication is completed in the browser and still resolves to either the authenticated SHVYA Superadmin role or an enabled Organization Admin role; VS Code itself never receives additional SHVYA authority.

## Authorization model

### SHVYA Superadmin

A Superadmin OAuth token is bound to the authenticated SHVYA Superadmin human.

It does not automatically expose customer data. Before any customer-specific tool can run, the Superadmin must select one explicit organization support context. Selecting or clearing support context requires `operations.read` but does not grant customer write authority; mutation tools separately require `operations.write`. Switching context closes the previous support session.

While a Superadmin support context is active:

- all customer-specific queries are forced into that organization;
- organization users can see that a SHVYA Support context remains open across the shared dashboard workspace, including Connect Hub / SHVYA API, until it is explicitly cleared/revoked; recent activity within the last 15 minutes is shown separately;
- the Superadmin organization page also shows the live support session;
- tool calls are linked to the support session in the Operations audit ledger.

From the Superadmin organization page, SHVYA can individually revoke Organization Admin external-AI grants, force-end open Superadmin support contexts, and review recent tenant Operations audit events. These controls do not expose raw OAuth token values.

### Organization Admin

An Organization Admin OAuth token is bound to:

- the authenticated SHVYA user;
- that user's current organization;
- the Organization Admin role;
- the Superadmin-owned Operations policy for that organization.

Organization Admin cannot select or override another tenant.

If Superadmin disables External AI Operations for the organization, all active Organization Admin Operations tokens for that tenant are revoked immediately. Re-enabling access requires fresh OAuth authorization; old tokens do not revive. Capability checks are re-evaluated on every request.

Organization users/agents are not offered an Operations OAuth identity.

Organization Admins can review and revoke active tenant-bound external-AI grants from the SHVYA Connect Hub without seeing access or refresh-token material. When `audit.read` is granted, the same panel also shows the latest tenant-scoped safe Operations audit metadata.

## Capability policy

Superadmin can independently grant:

- `organization.read`
- `diagnostics.read`
- `audit.read`
- `lead.stage.write`
- `lead.attributes.write`
- `ai.config.write`
- `crm.pipeline.config.write`
- `crm.stage.config.write`
- `crm.attribute.config.write`
- `automation.workflow.config.write`
- `automation.cadence.config.write`
- `automation.messaging.config.write`
- `configuration.plan.write`

Legacy stored `crm.config.write` and `automation.config.write` values are recognized only for backward compatibility and are expanded into these granular controls.

Write capabilities may require explicit human approval. Superadmin customer-state writes always use the approval gate.

An OAuth write scope does not bypass capability policy. Authenticated `tools/list` is also policy scoped: Organization Admin clients only discover tools for capabilities currently enabled by Superadmin, while Superadmin retains the full Operations surface.


## Current staging tool surface

The staging implementation defines **76 Operations-native tools** in addition to the existing read-only Diagnostic MCP tools exposed through the Operations authorization boundary. Authenticated `tools/list` is capability scoped.

### Context and inspection

- `get_operations_context`
- `list_organizations`
- `select_organization_context`
- `clear_organization_context`
- `get_organization_configuration`
- `get_ai_configuration`
- `get_knowledge_health`
- `get_automation_configuration`
- `get_messaging_automation_settings`
- `get_conversion_analysis`
- `get_operations_audit`

### Lead and qualification

- `diagnose_lead_qualification`
- `move_lead_stage`
- `repair_qualification_stage`
- `update_lead_attributes`
- `get_qualification_configuration`
- `validate_qualification_configuration`
- `upsert_qualification_configuration`

### AI, CRM and messaging configuration

- `update_ai_configuration`
- `upsert_pipeline_configuration`
- `upsert_stage_configuration`
- `upsert_attribute_configuration`
- `update_messaging_automation_settings`
- `get_content_authoring_policy`
- `list_whatsapp_accounts`
- `list_whatsapp_templates`
- `get_whatsapp_template_status`
- `create_whatsapp_template`
- `submit_whatsapp_template`
- `submit_whatsapp_templates`
- `validate_whatsapp_routing`
- `bind_whatsapp_account_to_pipeline`
- `begin_whatsapp_connection`

### Workflows, Cadence and simulations

- `upsert_workflow_configuration`
- `upsert_cadence_configuration`
- `add_cadence_step`
- `add_hosted_whatsapp_step`
- `update_cadence_step`
- `delete_cadence_step`
- `reorder_cadence_steps`
- `list_workflow_triggers`
- `list_workflow_actions`
- `get_workflow_schema`
- `validate_workflow_configuration`
- `simulate_ai_conversation`
- `simulate_workflow`
- `simulate_cadence`

### Touchpoints, FAQs and knowledge lifecycle

- `list_touchpoints`
- `upsert_touchpoint`
- `archive_touchpoint`
- `list_faqs`
- `upsert_faq`
- `archive_faq`
- `create_knowledge_source`
- `upload_knowledge_document`
- `publish_knowledge_document`
- `archive_knowledge_document`

### Configuration plans and lifecycle

- `get_configuration_dependency_graph`
- `validate_organization_configuration`
- `reorder_stages`
- `export_organization_configuration`
- `create_configuration_plan`
- `apply_configuration_plan`
- `rollback_configuration_plan`
- `import_organization_configuration`
- `archive_stage`
- `delete_stage`
- `archive_attribute`
- `delete_attribute`
- `archive_pipeline`
- `archive_cadence`
- `archive_workflow`

Configuration plans use the dedicated `configuration.plan.write` capability. Lifecycle operations validate dependencies and prefer reversible archive behavior where hard deletion would be unsafe.

### Superadmin diagnostics

- `test_integration_connection`
- `compare_organization_configuration`
- `get_configuration_integrity_diagnostics`
- `test_ai_response_policy`

The configuration comparison is Superadmin-only and opaque: it reports safe drift without returning credentials or raw secret values. Integrity diagnostics cover duplicate/orphan-style configuration problems, and AI response-policy testing validates bounded customer-facing behavior without turning diagnostic content into authority.


## Read / diagnostic tools

Operations exposes the existing tenant-scoped diagnostic tools through the Operations authorization boundary:

- find leads
- lead snapshot
- conversation
- message trace
- AI diagnostics
- integration health
- Workflow trace
- recent errors, including tenant-safe signed Instagram webhook failure evidence without raw webhook payloads
- runtime health

Operations-specific inspection also includes:

- authenticated role / organization / effective capabilities
- organization business + CRM + AI configuration
- full bounded AI/Playbook configuration view up to SHVYA's canonical 100,000-character Playbook limit when the summary is truncated
- organization knowledge/RAG health metadata
- Workflow and Cadence definitions
- pipeline-linked messaging automation settings
- qualification diagnosis
- equal-period conversion analysis
- Operations audit history
- Superadmin organization discovery

The conversion analysis distinguishes measured values from likely contributors and does not assert causality from correlation. Where SHVYA does not have a canonical historical aggregate, the tool reports that limitation instead of fabricating a metric.

## Mutation tools

Bounded mutation surfaces include:

- qualification configuration read/validate/upsert and completion-target controls
- Hosted/API WhatsApp discovery, connection start, pipeline binding and routing validation
- Meta WhatsApp template discovery, draft creation, single submission, and batch submission (up to 50 templates per call) through each template's selected connected WABA; Meta validation/approval remains authoritative
- customer-facing Cadence, Touchpoint and WhatsApp template authoring is normalized to plain text; only placeholders returned by `get_content_authoring_policy` are accepted, including active organization attribute keys
- saved Touchpoint and FAQ create/update/archive lifecycle
- knowledge source/document create, upload, publish and archive lifecycle
- Workflow schema discovery/validation plus Workflow/Cadence simulation
- configuration export/import, dependency graph, full validation, stage reordering, create/apply/rollback plans
- safe archive/delete lifecycle for stages, attributes, pipelines, Cadence and Workflows
- Superadmin integration, drift, integrity and AI response-policy diagnostics

- lead pipeline/stage transition
- completed-qualification → Qualified reconciliation
- existing non-sensitive lead attribute values
- organization AI profile / AI Playbook
- CRM pipeline configuration
- CRM stage configuration
- CRM custom-attribute configuration
- Workflow create/update
- Cadence create/update
- Cadence step creation
- pipeline-linked WhatsApp messaging automation settings

The tools reuse existing SHVYA service/validation contracts wherever available.

Examples:

- lead stage movement uses `services.crm.lead_transition`;
- lead/custom-attribute writes use `services.crm.attribute_service`;
- Workflow configuration uses the canonical Workflow validator;
- Cadence configuration and steps reuse `services.followup_service`.

Protected stages, tenant ownership, active pipeline/stage rules, target-stage required CRM attributes, WhatsApp account constraints, approved template requirements, sensitive attributes and other backend rules remain enforced. Generic Operations stage movement cannot bypass qualification: a move to Qualified must match SHVYA's authoritative qualification execution contract, configured criteria, and completion target.

## Dry-run and approval

Significant writes use this sequence:

1. validate tenant and capability;
2. require a specific action reason;
3. dry-run by default;
4. return proposed change, risk/reversibility where relevant and whether approval is required;
5. when approval is required, return an immutable `approval_event_id` tied to that actor, organization, tool, capability and exact proposal;
6. apply only when the caller explicitly supplies `dry_run=false`, `approved=true` and the same unexpired approval event ID;
7. reject mismatched, expired or already-used approval receipts;
8. re-read / verify the resulting state;
9. report `FIXED` only after verification.

Approval receipts expire after 30 minutes and are atomically single-use at execution-attempt time. Once an approved attempt claims a receipt, a fresh dry-run is required for any later attempt even if the first attempt does not complete successfully. Approved writes re-lock authoritative rows and compare the backend-resolved proposal again under the lock; if a human or another process changed the relevant lead/configuration after the dry-run, execution stops and requires a fresh dry-run instead of overwriting newer state. A dry-run does not create CRM, AI-profile or policy state.

## Audit

Every authenticated Operations tool call creates an `OperationsAuditEvent`. Approval-required executions also create a unique append-only `OperationsApprovalUse` claim against the dry-run audit event before mutation begins, preventing concurrent or later receipt replay.

The audit stores bounded metadata:

- actor
- SHVYA role
- organization/support session
- tool
- capability
- target reference
- specific reason
- outcome
- request fingerprint
- safe change summary
- duration / safe error code

It does not store raw OAuth tokens, provider credentials, raw tool arguments, full conversations or hidden model reasoning.

Organization audit reads are always limited to the explicitly active tenant. Organization-facing audit views replace internal Superadmin support-context reasons with fixed customer-safe wording while retaining the immutable internal audit event for Superadmin review. SHVYA Superadmin can separately request `scope=platform` to review only tenantless platform/OAuth lifecycle events (`organization IS NULL`); that scope never aggregates customer organization audit rows.

Operations audit rows are append-only at both model-instance and queryset ORM boundaries; normal `.save()`, instance `.delete()`, bulk `.update()` and queryset `.delete()` rewrites are blocked.

## Untrusted content and secrets

Customer/lead content is data, never authorization. This includes:

- WhatsApp / Instagram messages
- notes
- support-ticket text
- imported files
- uploaded documents
- Playbook/customer prompts
- webhook text

The Operations response sanitizer redacts bearer tokens, SHVYA API keys, inline credential assignments, database URLs, long credential-like values and private-key blocks.

No Operations tool exposes raw environment variables, database credentials, Django secrets, provider access tokens, AI provider keys, SMTP passwords, JWT signing material or encryption keys. Sensitive CRM attribute definitions are omitted from organization configuration reads, sensitive Workflow attribute conditions/actions are redacted, and Operations AI-configuration writes reject credential-like material instead of storing it for later prompt use.

## Relationship to Diagnostic MCP

Do not add writes to the existing Diagnostic MCP.

Diagnostic MCP remains:

- organization/API-key scoped;
- read-only;
- independently revocable at the OAuth grant or underlying API-key boundary;
- protected by strict PKCE S256 validation, fixed refresh-grant lifetime and hashed access/refresh material;
- CIMD-capable with DCR fallback for compatible External AI clients;
- audited through immutable tenant-scoped access-log metadata;
- suitable for troubleshooting without CRM mutation authority.

Operations MCP is a separate authorization, policy, support-context and audit boundary.

## Primary implementation files

- `apps/integrations/operations_models.py`
- `apps/integrations/operations_policy.py`
- `apps/integrations/operations_auth.py`
- `apps/integrations/operations_agent_prompt.py`
- `apps/integrations/operations/registry.py`
- `apps/integrations/operations/tool_catalog.py`
- `apps/integrations/operations/tools/`
- `apps/integrations/operations/configuration/`
- `apps/integrations/operations/diagnostics/`
- `apps/integrations/operations_tools.py` and the historical
  `operations_tool_*`, `operations_extended_tools.py` and
  `operations_configuration_management.py` compatibility entry points
- `apps/integrations/operations_lifecycle.py`
- `apps/integrations/operations_superadmin_diagnostics.py`
- `apps/integrations/mcp_schema.py`
- `apps/integrations/views/operations_mcp.py`
- `apps/integrations/migrations/0006_operations_mcp.py` through the current Operations configuration-plan migrations
- `apps/superadmin/operations_views.py`
- `templates/integrations/operations_authorize.html`
- `apps/integrations/tests/operations_mcp_test_base.py`
- `apps/integrations/tests/test_operations_mcp_*.py`
- `apps/integrations/tests/test_operations_configuration_management.py`
- `apps/integrations/tests/test_operations_lifecycle.py`
- `apps/integrations/tests/test_operations_additional_diagnostics.py`

The Diagnostic MCP implementation remains under `apps/integrations/diagnostic_*.py` and `apps/integrations/views/mcp.py`.

## Company setup through the existing MCP endpoint

The Shvya setup kit is bundled into `/operations/mcp/`; no local companion server,
new credential or separate AI runtime is required. See [setup workflow and rollout](operations-mcp-setup.md).
The endpoint supports authenticated `prompts/list`, `prompts/get`, `resources/list`
and `resources/read`, plus equivalent library tools for clients that only support tools.

Superadmin **Allowed capabilities** includes four independent controls:

| Capability | Permitted work |
| --- | --- |
| `setup.library.read` | Read bundled setup/review skills, agent prompts, references and variables |
| `setup.artifacts.prepare` | Prepare Playbook/About/voice drafts and analyze supplied group exports; no persistence |
| `setup.intake.read` | Read the selected organization's source-attributed setup notes |
| `setup.intake.write` | Create/update/archive intake with write scope, reason, dry-run and approval |

Existing organization defaults and OAuth grants are unchanged. Enable the desired
capabilities in policy, then reconnect the MCP client with fresh OAuth consent.
Intake is separate from AI Brain publication, and the original AI/CRM/knowledge/
Workflow/Cadence write capabilities still apply to those configuration changes.


## Transaction and audit guarantee

Approval-required writes are bound to the exact backend-resolved dry-run proposal. Mutation paths re-lock authoritative rows, re-check the proposal under lock, write, and verify before commit. Successful customer-state writes and their Operations audit event commit atomically; if audit persistence fails, the customer-state mutation rolls back. Failed/denied attempts still produce their bounded audit event, and an approval receipt already claimed by an execution attempt remains single-use.

OAuth authorization-code issuance and successful token issue/refresh also commit only with their lifecycle security audit. Security revocation remains fail-secure: revoking access is prioritized even when the action is initiated from dashboard/session controls.


### Knowledge / RAG health

`get_knowledge_health` provides organization-scoped metadata only: knowledge source type/name, URL hostname, document version/status/publication state, chunk counts, embedding coverage and timestamps. It deliberately does not return document/chunk text, file bytes, source keys, signed URL query strings or embedding vectors. Raw ingestion-error text, stored source-key/file values and share instructions are deferred from the MCP presentation query; only bounded status booleans are projected. The organization configuration summary includes the same bounded knowledge-health view so “understand my business” can distinguish complete grounded context from missing/failed knowledge without broad source retrieval.

### Operations attachment transport

Operations MCP keeps ordinary/public requests bounded to 1 MiB, while authenticated
Operations requests may carry larger attachment payloads up to a 72 MiB JSON request
body. The production/staging Nginx exception is scoped only to `/operations/mcp/`
at 80 MiB; ordinary dashboard routes retain their existing 20 MiB request limit.

- Hosted WhatsApp Cadence attachments use the canonical Hosted service limit of 50 MiB.
- Email Cadence steps support up to 5 attachments with an 18 MiB combined decoded size.
- Email attachment content is stored in SHVYA file storage and is not written into the
  Operations audit ledger. Approval proposals include bounded filename/MIME/size/SHA-256
  metadata so a reviewed binary cannot be silently replaced before execution.
- Email attachment delivery reuses the organization's connected SMTP mailbox and the
  existing `send_organization_email(..., attachments=...)` path.

### Messaging automation exposure

Messaging Operations returns only the canonical automation controls used by SHVYA. Legacy/internal session JSON keys are not surfaced, and WhatsApp provider access-token fields are deferred from MCP read and locked-write query paths. The backend pipeline mapping and canonical messaging service remain authoritative for validation, persistence and verification.


## Consent-bound capability snapshot

Organization Admin authorization is bounded by three layers:

- **Live Superadmin policy** — the maximum capabilities currently enabled for the organization.
- **OAuth granted capabilities** — the exact capability snapshot consented to when that external-AI grant was authorized.
- **Effective capabilities** — the intersection of live policy, the grant snapshot, and OAuth read/write scope.

A policy reduction takes effect immediately on existing grants. A later policy expansion does **not** silently give an already-connected ChatGPT, Claude or VS Code client new authority; the Organization Admin must complete fresh SHVYA OAuth authorization. The MCP context reports `policy_capabilities`, `granted_capabilities` and current `capabilities` separately.

The Superadmin policy is granular for customer-state writes: lead stage, lead attributes, AI/Playbook, CRM pipeline, CRM stage, CRM attribute, Workflow, Cadence and messaging-automation settings can be controlled independently. Legacy stored `crm.config.write` and `automation.config.write` policies are expanded only for backward compatibility and are normalized to granular controls when Superadmin saves the policy.
