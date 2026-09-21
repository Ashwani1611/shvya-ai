# SHVYA Operations MCP

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

VS Code can connect to the remote Streamable HTTP endpoint at `/operations/mcp/` and use the same actor-bound OAuth flow. The server supports Dynamic Client Registration and the exact redirect URLs required by VS Code's MCP OAuth flow.

A typical VS Code MCP configuration points an HTTP server entry at the deployed `https://<host>/operations/mcp/` URL. Authentication is completed in the browser and still resolves to either the authenticated SHVYA Superadmin role or an enabled Organization Admin role; VS Code itself never receives additional SHVYA authority.

## Authorization model

### SHVYA Superadmin

A Superadmin OAuth token is bound to the authenticated SHVYA Superadmin human.

It does not automatically expose customer data. Before any customer-specific tool can run, the Superadmin must select one explicit organization support context. Selecting or clearing support context requires `operations.read` but does not grant customer write authority; mutation tools separately require `operations.write`. Switching context closes the previous support session.

While a Superadmin support context is active:

- all customer-specific queries are forced into that organization;
- organization users can see that SHVYA Support is active across the shared dashboard workspace, including Connect Hub / SHVYA API, while the support session has had activity within the last 15 minutes;
- the Superadmin organization page also shows the live support session;
- tool calls are linked to the support session in the Operations audit ledger.

### Organization Admin

An Organization Admin OAuth token is bound to:

- the authenticated SHVYA user;
- that user's current organization;
- the Organization Admin role;
- the Superadmin-owned Operations policy for that organization.

Organization Admin cannot select or override another tenant.

If Superadmin disables External AI Operations for the organization, all active Organization Admin Operations tokens for that tenant are revoked immediately. Re-enabling access requires fresh OAuth authorization; old tokens do not revive. Capability checks are re-evaluated on every request.

Organization users/agents are not offered an Operations OAuth identity.

## Capability policy

Superadmin can independently grant:

- `organization.read`
- `diagnostics.read`
- `audit.read`
- `lead.stage.write`
- `lead.attributes.write`
- `ai.config.write`
- `crm.config.write`
- `automation.config.write`

Write capabilities may require explicit human approval. Superadmin customer-state writes always use the approval gate.

An OAuth write scope does not bypass capability policy. Authenticated `tools/list` is also policy scoped: Organization Admin clients only discover tools for capabilities currently enabled by Superadmin, while Superadmin retains the full Operations surface.

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
- Workflow and Cadence definitions
- qualification diagnosis
- equal-period conversion analysis
- Operations audit history
- Superadmin organization discovery

The conversion analysis distinguishes measured values from likely contributors and does not assert causality from correlation. Where SHVYA does not have a canonical historical aggregate, the tool reports that limitation instead of fabricating a metric.

## Mutation tools

Bounded mutation surfaces include:

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

Approval receipts expire after 30 minutes and are atomically single-use at execution-attempt time. Once an approved attempt claims a receipt, a fresh dry-run is required for any later attempt even if the first attempt does not complete successfully. A dry-run does not create CRM, AI-profile or policy state.

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
- independently revocable;
- suitable for troubleshooting without CRM mutation authority.

Operations MCP is a separate authorization, policy, support-context and audit boundary.

## Primary implementation files

- `apps/integrations/operations_models.py`
- `apps/integrations/operations_policy.py`
- `apps/integrations/operations_auth.py`
- `apps/integrations/operations_agent_prompt.py`
- `apps/integrations/operations_tools.py`
- `apps/integrations/views/operations_mcp.py`
- `apps/integrations/migrations/0006_operations_mcp.py`
- `apps/superadmin/operations_views.py`
- `templates/integrations/operations_authorize.html`
- `apps/integrations/tests/test_operations_mcp.py`

The Diagnostic MCP implementation remains under `apps/integrations/diagnostic_*.py` and `apps/integrations/views/mcp.py`.


## Transaction and audit guarantee

Approval-required writes are bound to the exact backend-resolved dry-run proposal. Mutation paths re-lock authoritative rows, re-check the proposal under lock, write, and verify before commit. Successful customer-state writes and their Operations audit event commit atomically; if audit persistence fails, the customer-state mutation rolls back. Failed/denied attempts still produce their bounded audit event, and an approval receipt already claimed by an execution attempt remains single-use.

OAuth authorization-code issuance and successful token issue/refresh also commit only with their lifecycle security audit. Security revocation remains fail-secure: revoking access is prioritized even when the action is initiated from dashboard/session controls.


### Knowledge / RAG health

`get_knowledge_health` provides organization-scoped metadata only: knowledge source type/name, URL hostname, document version/status/publication state, chunk counts, embedding coverage and timestamps. It deliberately does not return document/chunk text, file bytes, source keys, signed URL query strings or embedding vectors. The organization configuration summary includes the same bounded knowledge-health view so “understand my business” can distinguish complete grounded context from missing/failed knowledge without broad source retrieval.


## Consent-bound capability snapshot

Organization Admin authorization is bounded by three layers:

- **Live Superadmin policy** — the maximum capabilities currently enabled for the organization.
- **OAuth granted capabilities** — the exact capability snapshot consented to when that external-AI grant was authorized.
- **Effective capabilities** — the intersection of live policy, the grant snapshot, and OAuth read/write scope.

A policy reduction takes effect immediately on existing grants. A later policy expansion does **not** silently give an already-connected ChatGPT, Claude or VS Code client new authority; the Organization Admin must complete fresh SHVYA OAuth authorization. The MCP context reports `policy_capabilities`, `granted_capabilities` and current `capabilities` separately.

The Superadmin policy is granular for customer-state writes: lead stage, lead attributes, AI/Playbook, CRM pipeline, CRM stage, CRM attribute, Workflow, Cadence and messaging-automation settings can be controlled independently. Legacy stored `crm.config.write` and `automation.config.write` policies are expanded only for backward compatibility and are normalized to granular controls when Superadmin saves the policy.
