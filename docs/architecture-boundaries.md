# Architecture boundaries and migration plan

This document records module-boundary rules that are intentionally stricter than
simple Python importability. The goal is to prevent SHVYA from growing parallel
implementations while preserving stable Django migration identities.

## Service ownership

New business logic should be placed with the domain that owns it:

- CRM-specific logic: `apps/crm/services/` or an established canonical CRM
  service when the existing root service is already the shared contract.
- AI engagement logic: `apps/ai_engagement/services/`.
- Integrations/Operations logic: `apps/integrations/operations/`, with stable
  compatibility entry points in the historical `apps/integrations/operations_*`
  modules.
- Sales, Calendar, Support and Telephony logic remain inside their owning apps.

The root `services/` tree is reserved for established cross-domain application
contracts and provider adapters that are already shared by multiple apps. Do not
create a second implementation in `services/` when the owning app already has a
service boundary.

When touching a grandfathered root service, prefer moving behavior toward the
owning app only when the migration can preserve imports and test coverage. Do not
perform namespace churn merely for aesthetics.

## Operations MCP

`apps/integrations/operations_tools.py` is a compatibility facade. Its public
symbols remain stable for the MCP transport, configuration-plan engine, extended
tools and tests.

Implementation is split by responsibility:

- `operations/registry.py` and `operations/tool_catalog.py` — canonical tool
  discovery and registration.
- `operations/tools/` — domain-oriented CRM, qualification, messaging, Cadence,
  Workflow, knowledge, FAQ and configuration tools.
- `operations/configuration/` — validation, export, import and plan execution.
- `operations/diagnostics/` — lead, messaging and runtime diagnostics.
- `operations/setup_assets/` and `operations/setup_library.py` — immutable MCP prompts/resources, 25 domain skills, setup/review sub-prompts and deterministic authoring templates; these are guidance and never an authorization layer.
- `operations_lifecycle.py`, `operations_policy.py`, `operations_auth.py` and
  `views/operations_mcp.py` — lifecycle, capability, actor-bound authorization
  and protocol transport.

`operations_tool_read.py`, `operations_tool_actions.py`,
`operations_tool_config.py`, `operations_extended_tools.py` and
`operations_configuration_management.py` remain compatibility facades for
existing imports and patch seams; new behavior belongs in `operations/`.

New Operations tools should be added to the focused owner rather than growing the
facade back into a monolith. New reusable reasoning belongs in the narrow domain
skill and manifest, not as duplicated transport logic. Keep read-only diagnosis,
mutation-capable repair and final acceptance testing as separate skill concerns.

## AI task and qualification boundaries

`apps/ai_engagement/tasks.py` preserves the public Celery task names and queue
contracts. Focused task orchestration lives in `apps/ai_engagement/task_handlers/`
and reusable engagement execution lives in
`apps/ai_engagement/services/engagement_execution.py`.

`qualification_execution_contract.py` remains the stable qualification contract.
Planning, evidence validation, completion, reconciliation and finalization live in
`apps/ai_engagement/services/qualification_execution/`. Do not introduce another
qualification state machine or bypass the tenant-scoped CRM execution contracts.

## AI runtime patch ceiling

The current AI runtime still contains compatibility installers accumulated during
rapid hardening. The approved installer set is frozen by
`test_runtime_cleanup_contract.py`. Adding another process-wide installer should
fail that contract until an explicit architecture review updates the approved set.

Preferred direction for future work:

1. Put behavior into the owning service or graph node directly.
2. Delete the corresponding installer once all callers use the canonical path.
3. Preserve the existing engagement/qualification contracts and regression tests.
4. Avoid replacing class/module methods at Django startup when a direct call or
   dependency can express the same behavior.

## `apps.channels` rename plan

The project has a historical Django app at `apps.channels` whose app label is
`channels`. The third-party Django Channels package has the same default label.
The current runtime therefore imports the third-party package as a library and
does not add it to `INSTALLED_APPS`.

Renaming the live SHVYA app module is desirable eventually, but changing the
Django app label in-place is not safe because migration dependencies, content
types, permissions and table names use that identity.

A future rename should use this sequence:

1. Pick a neutral Python module name such as `apps.messaging`.
2. Keep `AppConfig.label = "channels"` during the first migration phase so
   existing database tables and migration history remain authoritative.
3. Move Python imports/templates/static references to the new module path while
   preserving the label.
4. Run the full Django migration graph, content-type/permission checks, webhook,
   WebSocket and Celery tests on staging.
5. Only consider changing the Django app label/table identities in a separate,
   explicitly reviewed data migration if there is a concrete operational need.
6. Promote staging to main only after zero migration drift and complete messaging
   regression coverage.

Until that migration is scheduled, new code should not deepen the naming collision
or create another messaging app.
