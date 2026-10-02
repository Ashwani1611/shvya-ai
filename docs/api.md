# SHVYA API and integration surface

> **Implementation note:** updated for the current Operations MCP surface on 2026-10-03. Source code, Django models/migrations, tests, runtime configuration and authenticated MCP discovery remain authoritative.

This document is the concise route map for the current application. Django URL configuration and view/service tests remain authoritative.

## Authentication boundaries

| Boundary | Typical prefix | Authentication |
| --- | --- | --- |
| Dashboard web | `/dashboard/` | Dedicated CRM session and tenant authorization |
| Superadmin web | `/superadmin/` | Dedicated SHVYA Superadmin session |
| Django admin | `/admin/` | Django admin session |
| Application API | `/api/v1/` | JWT or the endpoint's established organization/API-key contract |
| Provider webhooks | `/webhooks/` | Provider signature/token verification before tenant routing |
| Diagnostic MCP | `/mcp/` | Read-only diagnostic OAuth/API-key boundary |
| Operations MCP | `/operations/mcp/` | Actor-bound OAuth, capability policy and explicit tenant context |

Never infer tenant scope from a request body when authenticated context already determines it.

## Core API map

### Authentication

- `POST /api/v1/auth/token/`
- `POST /api/v1/auth/token/refresh/`

### CRM

The current routing exposes the CRM lead API directly below `/api/v1/leads/` and through the versioned router below `/api/v1/crm/`.

Important CRM operations include lead list/create paths, `upsert/`, and `bulk/move-stage/`. Canonical lead transition and attribute services remain authoritative for writes.

### Teams and Sales Desk

- `/api/v1/teams/` — team and membership APIs.
- `/api/v1/copilot/` — internal Sales Desk/Copilot flags, configuration and lead stage action API.

Historical implementation names such as `copilot` can remain in routes even though customer-facing navigation uses **Sales Desk**.

### AI / knowledge

- `/api/v1/ai-engagement/` — organization AI/knowledge APIs.
- `/api/v1/knowledge/` — the versioned knowledge include.

Current AI-engagement routes cover organization info, FAQs, documents, reindexing, knowledge sources, bounded dashboard deletion and the playground.

### Call Intelligence / telephony

The same telephony API set is available below both `/api/v1/call-intelligence/` and `/api/v1/telephony/`.

Current routes:

- `devices/register/`
- `devices/heartbeat/`
- `events/`
- `calls/`
- `calls/<call_id>/media/`
- `calls/<call_id>/notes/`
- `calls/<call_id>/follow-up/`
- `analytics/`
- `dispositions/`
- `settings/`

Call ingestion is idempotent on organization + source + source-call identity and reuses tenant-safe CRM services.

## Provider webhooks

- `/webhooks/whatsapp/`
- `/webhooks/instagram/`
- `/webhooks/meta-leads/`

Webhook handlers must authenticate/verify the provider boundary before resolving account, organization, pipeline or lead state. Historical synchronization must not be treated as a new live inbound event.

## Operations MCP and OAuth

Current Operations endpoints:

- `/operations/mcp/`
- `/operations/oauth/register`
- `/operations/oauth/authorize`
- `/operations/oauth/token`
- `/operations/oauth/revoke`
- `/operations/.well-known/oauth-protected-resource`
- `/.well-known/oauth-authorization-server/operations`
- `/.well-known/oauth-protected-resource/operations/mcp/`

See [`operations-mcp.md`](./operations-mcp.md) for roles, capabilities, the current **101 Operations-native tools + 10 diagnostic tools**, dry-run/approval behavior and audit guarantees. The same endpoint also serves the **25 top-level domain skills** through authenticated prompts/resources; see [`operations-mcp-setup.md`](./operations-mcp-setup.md).

## Public booking surface

SHVYA Calendar uses public web routes rather than pretending bookings are a generic REST API:

- `/calendar/<public_id>/<slug>/`
- submit and schedule routes under that page
- booking confirmation
- reschedule
- cancel

See [`shvya-calendar-workspace.md`](./shvya-calendar-workspace.md).

## API contract rules

1. Keep organization scope on every tenant-owned lookup.
2. Reuse canonical services for business writes.
3. Validate related rows belong to the same organization.
4. Keep provider/external effects idempotent and retry safe.
5. Do not return secrets, credential ciphertext, raw tokens, hidden prompts or another tenant's data.
6. Preserve stable error contracts and authorization checks when adding routes.
7. Update this document and tests when the public/connector route contract changes.

## Source-of-truth files

- `config/urls.py`
- `api/v1/urls.py`
- `apps/*/urls*.py`
- `apps/integrations/urls/diagnostics.py`
