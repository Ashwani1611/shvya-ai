# Workflows runtime and verification contract

Workflows reuse the existing CRM, Cadence, connected-mailbox and WhatsApp transports. They do not bypass tenant permissions, account health or messaging restrictions.

## Trigger and action coverage

The regression suite `apps/triggers/tests/test_workflows_runtime_contract.py` exercises the Cartesian product of all seven registered triggers and all nine actions using real Django models/PostgreSQL. It validates saved rules, evaluates durable events, executes actions, asserts database outcomes and checks repeated execution. Provider/network calls are mocked; passing this suite is not a claim of live-provider acceptance or zero possible bugs.

Triggers: stage moved, sequence ended, lead created, no response, keyword, stage idle and manual call logged. Actions: start/stop sequence, move stage, schedule WhatsApp, send connected-mailbox email, set reminder, set attribute, lead AI toggle and lead follow-up toggle.

The existing signal, timer, tenant, permission, CSRF, cooldown, ordering and connected-mailbox tests remain part of repository CI. New tests additionally cover history suppression, saved-state checks, source matching, delivery leases, safe disable, cancellation and uncertain delivery.

## Built-in Source

Every trigger exposes Source as a fixed condition, separate from custom attribute conditions. Choices come from `Lead.lead_source`; a custom SOURCE answer never overrides them. Empty selection means any source. Selected sources are alternatives (OR), combined with pipeline/stage/custom conditions (AND). The existing stored creation origin is read after inbound normalization; this feature does not rewrite existing leads or add a parallel source field.

## Sequence-ended selection

Selecting A sequence ends reveals a native sequence dropdown below the trigger. Additional dropdowns preserve existing multi-sequence rules. Any selected completed sequence can match. Empty pipeline scopes are allowed for this trigger; other triggers still require pipeline/stage scope.

## Deferred delivery safety

Pending, scheduled and queued runs have no completion timestamp. Relative schedules anchor to the triggering event time. The canonical WhatsApp sender rechecks workflow enablement, active organization, tenant/lead/sender identity, timer validity, account connectivity and API reply window on each task attempt. Hosted sends use the existing Hosted adapter and Account Health protections, not Meta's free-text-window rule.

Dispatch uses the same durable message and a ten-minute lease so repeated Beat passes do not reset the sender's bounded retry backoff. Explicit temporary provider errors retain bounded retry; exhausted attempts stop. Unknown provider outcomes and stale in-flight sends require review rather than automatic replay. Connected-mailbox emails similarly use an at-most-once claim and expose interrupted/uncertain delivery for review.

Disabling a workflow remains possible even when a saved dependency was deleted. Re-enabling validates its configuration. Manual messaging and unrelated AI/bulk contracts remain unchanged.

## Release validation

Run the standard repository lint, migration check, Django check, full tests with coverage, Compose validation and both Docker builds. Verify the staging deployment before a focused main promotion. Verify main CI and the exact production deployment SHA/readiness afterward. Do not ship temporary development helpers or unrelated staging changes.
