# SHVYA Operations capability expansion

This document describes the production-safe capability layer added around the
existing Operations MCP. It excludes Voice Agent and Vault; both remain
separate products.

## Operating contract

Every organization-scoped operation follows this sequence:

```text
OAuth actor -> get_operations_context -> explicit tenant context
    -> capability + scope check -> JSON schema validation
    -> tenant read/lock -> dry-run preview -> approval event
    -> atomic write -> readback verification -> immutable audit event
```

`get_operations_context` is the required first call before organization data is
read or changed. Superadmin access still requires an explicit selected support
context. Organization admins remain pinned to their assigned organization.

No capability returns credentials, access/refresh tokens, SMTP passwords,
webhook secrets, raw provider payloads, signed media URLs, or Vault data.

## Capability discovery

The authenticated discovery endpoint is:

```http
GET /operations/capabilities
Authorization: Bearer <actor-bound-token>
```

The same payload is available through the MCP tool `get_capability_discovery`.
The response contains:

```json
{
  "role": "SHVYA_SUPERADMIN",
  "organization": {"id": "...", "name": "...", "active": true},
  "oauth_scopes": ["operations.read", "operations.write"],
  "granted_capabilities": ["calendar.config.write"],
  "effective_capabilities": ["calendar.config.write"],
  "tools": [
    {
      "name": "reschedule_booking",
      "available": true,
      "capability": "calendar.config.write",
      "approval_required": true,
      "dependencies": ["operations.read", "operations.write"],
      "environment_limitations": []
    }
  ],
  "unsupported_features": [
    {"name": "Voice Agent", "status": "excluded_from_scope"},
    {"name": "Vault", "status": "excluded_from_scope"}
  ],
  "global_dependencies": [],
  "environment_limitations": []
}
```

The endpoint is `no-store` and requires OAuth. It reports live policy/grant
state, not a cached or inferred permission set.

## New tool schemas

All write schemas inherit `dry_run`, `approved`, `approval_event_id`, and a
specific `reason` from the existing Operations write contract.

| Capability | Tools | Side effects |
| --- | --- | --- |
| Onboarding | `prepare_account_onboarding`, `list_industry_playbooks` | Read-only; proposes phase order and template metadata |
| Trace investigation | `get_production_trace` | Read-only; content requires `trace.content.read` and is redacted/bounded |
| Calendar | `get_calendar_configuration`, `validate_calendar_configuration`, `upsert_calendar_configuration`, `verify_booking`, `reschedule_booking`, `update_booking_status` | Writes use approval; cancellation suppresses pending reminders |
| Integrations | `get_integration_lifecycle`, existing connect/test tools, `disconnect_integration` | Disconnect clears provider credentials while preserving history |
| Bulk Cadence | `validate_cadence_batch`, existing reorder/update tools and configuration plans | Validation only until an approved plan is applied |
| Acceptance | `run_acceptance_suite` | Deterministic/no-send; no provider calls or activation |
| Commitments | `list_commitments`, `upsert_commitment` | Operational task record only; no customer delivery |
| Team controls | `get_team_settings`, `upsert_team_settings` | Responder hours, AI ownership, handoff, sender identity and Co-Pilot settings; no sends or activation |

### Trace access model

`get_production_trace` always checks `organization_id` against the active
tenant. Metadata mode returns run identity, source/outbound message IDs,
pipeline/stage/account IDs, status, reason, model name, timing, and available
sections. Content mode additionally requires `trace.content.read` and returns
only bounded sanitized values for rendered prompt, model input/output, message
attribution, and run references. If a field is not persisted, it is absent; it
is never reconstructed or guessed.

### Calendar model

Calendar operations reuse `CalendarPage`, `CalendarBooking`, reminder delivery,
availability, reschedule, and Google sync services. Booking status changes are
limited to `cancelled`, `completed`, and `no_show`; terminal bookings cannot be
changed. Rescheduling rechecks availability and tenant ownership under the
canonical service. Provider sync failures are returned as state, not hidden.

### Integration lifecycle model

Inventory reports supported actions and current state for WhatsApp, email,
Google Calendar, Google Sheets, webhooks, and Instagram. Connect/reconnect
continues to use the provider-specific existing flows; the lifecycle layer
does not accept raw credentials. Disconnect is archive-first and history-safe:
it disables routing and clears stored secrets/tokens without deleting messages,
bookings, delivery records, or audit history.

## Validation and failure handling

- Cross-tenant IDs resolve to “not found in this organization”; no existence
  oracle is returned.
- Published Calendar pages require a pipeline/stage appropriate to the page
  type, positive slot rules, valid IANA timezone, and reminder coverage.
- Booking reschedules reject terminal bookings, stale or unavailable slots,
  invalid timezone values, and provider sync errors remain visible.
- Trace content is rejected unless the grant includes `trace.content.read`;
  sanitizer output is marked `response_sanitized`.
- Cadence batch validation flags duplicate ordering, equal-delay overlap,
  missing `{{lead_first_name}}`, and undeclared suppression. It never enrolls or
  sends.
- Commitments reject secret-like text, foreign owners, unsupported states, and
  malformed dates. Completion records `completed_at`.
- Integration disconnects require an approval receipt, lock the exact tenant
  resource, clear only supported credential fields, and verify the resulting
  state.
- Any unexpected handler error is converted to a safe Operations error and
  audit event. Raw exceptions and provider secrets are not returned.

## Acceptance test cases

The deterministic acceptance suite covers these cases and reports `passed` or
`blocked` with evidence:

1. required qualification questions and target stage are configured;
2. configured multilingual coverage exists;
3. refusal is treated as a no-send policy case;
4. pricing response is grounded in current AI Brain configuration;
5. explicit human handoff is supported;
6. opt-out suppression is supported;
7. an active Cadence exists and is structurally valid;
8. an active Workflow exists and is structurally valid;
9. at least one delivery integration is ready.

Provider delivery tests remain explicit and separate. They may validate auth or
readiness, but do not send customer messages unless a future provider-specific
tool explicitly documents that side effect and obtains authorization.

## Dependency diagram

```text
AI Brain/About + Knowledge + FAQs
          |             |
          v             v
  Qualification -> Attributes -> Stages/Pipelines
          |             |
          v             v
    Workflows <----> Cadences/Touchpoints
          |             |
          v             v
 Integrations ----> Calendar/Bookings ----> Reminders
          |
          v
  Acceptance suite -> Commitments / audit findings
```

The existing configuration-plan engine remains the migration and rollback
boundary for multi-object changes. New Calendar/integration/commitment writes
use the same approval and audit primitives; they are not silently folded into
an unrelated plan.

## Phased implementation plan

### Phase 1 — shared control plane (implemented)

Capability discovery, trace content gating, Calendar validation/booking
verification, lifecycle inventory, safe disconnect, onboarding readiness,
industry templates, no-send acceptance coverage, and commitments.

### Phase 2 — provider-complete lifecycle

Add provider-specific connect/reconnect/configure handlers for Google Calendar,
email, WhatsApp and supported providers behind the same schemas, with explicit
provider capability metadata and bounded live diagnostics.

### Phase 3 — onboarding execution

Turn the read-only onboarding proposal into a persisted, resumable orchestration
run that emits configuration-plan operations, tracks phase-level approvals, and
creates commitments for unresolved inputs. Existing plan apply/rollback remains
the only production configuration executor.

### Phase 4 — bulk migration and acceptance depth

Add resumable bulk Cadence/message migration with overlap resolution, suppression
preview, idempotent batch receipts, and rollback checkpoints. Expand acceptance
fixtures to execute canonical simulations for multilingual, refusal, pricing,
handoff, opt-out, Workflow, Cadence and delivery paths.

### Phase 5 — team-level controls

The first team/user settings surface is now available through
`get_team_settings` and `upsert_team_settings`. It reports and updates
responder hours, AI ownership, handoff rules, sender identity, pipeline owners,
and Co-Pilot behavior behind `team.settings.write`. Provider routing and lead
ownership remain tenant-scoped; future work can add per-team overrides and
approval delegation without changing the contract.
