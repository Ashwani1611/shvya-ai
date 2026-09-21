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
- Protected-resource metadata: `/operations/.well-known/oauth-protected-resource`
- Authorization-server metadata: `/.well-known/oauth-authorization-server/operations`

Operations OAuth uses PKCE S256. Access and refresh values are stored only as hashes. Public client redirect URIs are restricted to HTTPS ChatGPT/OpenAI and Claude/Anthropic hosts plus the exact VS Code MCP callbacks `http://127.0.0.1:33418` and `https://vscode.dev/redirect`. Arbitrary localhost ports/hosts and arbitrary `vscode.dev` paths are rejected.

## VS Code connection

VS Code can connect to the remote Streamable HTTP endpoint at `/operations/mcp/` and use the same actor-bound OAuth flow. The server supports Dynamic Client Registration and the exact redirect URLs required by VS Code's MCP OAuth flow.

A typical VS Code MCP configuration points an HTTP server entry at the deployed `https://<host>/operations/mcp/` URL. Authentication is completed in the browser and still resolves to either the authenticated SHVYA Superadmin role or an enabled Organization Admin role; VS Code itself never receives additional SHVYA authority.

## Authorization model

### SHVYA Superadmin

A Superadmin OAuth token is bound to the authenticated SHVYA Superadmin human.

It does not automatically expose customer data. Before any customer-specific tool can run, the Superadmin must select one explicit organization support context. Switching context closes the previous support session.

While a Superadmin support context is active:

- all customer-specific queries are forced into that organization;
- organization users can see that SHVYA Support is active across the shared dashboard workspace, including Connect Hub / SHVYA API;
- the Superadmin organization page also shows the live support session;
- tool calls are linked to the support session in the Operations audit ledger.

### Organization Admin

An Organization Admin OAuth token is bound to:

- the authenticated SHVYA user;
- that user's current organization;
- the Organization Admin role;
- the Superadmin-owned Operations policy for that organization.

Organization Admin cannot select or override another tenant.

If Superadmin disables External AI Operations for the organization, existing Organization Admin Operations tokens stop authorizing. Capability checks are re-evaluated on every request.

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

An OAuth write scope does not bypass capability policy.

## Read / diagnostic tools

Operations exposes the existing tenant-scoped diagnostic tools through the Operations authorization boundary:

- find leads
- lead snapshot
- conversation
- message trace
- AI diagnostics
- integration health
- Workflow trace
- recent errors
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

Protected stages, tenant ownership, active pipeline/stage rules, WhatsApp account constraints, approved template requirements, sensitive attributes and other backend rules remain enforced.

## Dry-run and approval

Significant writes use this sequence:

1. validate tenant and capability;
2. require a specific action reason;
3. dry-run by default;
4. return proposed change, risk/reversibility where relevant and whether approval is required;
5. apply only when the caller explicitly supplies `dry_run=false` and, when required, `approved=true`;
6. re-read / verify the resulting state;
7. report `FIXED` only after verification.

A dry-run does not create CRM, AI-profile or policy state.

## Audit

Every authenticated Operations tool call creates an `OperationsAuditEvent`.

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

No Operations tool exposes raw environment variables, database credentials, Django secrets, provider access tokens, AI provider keys, SMTP passwords, JWT signing material or encryption keys.

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
