# Smart Triggers

Smart Triggers is available at `/dashboard/smart-triggers/` through the existing dashboard sidebar. It uses Django templates and JavaScript, with Django services and Celery background processing.

## Supported behavior

Seven events are supported: lead creation, stage movement, sequence completion, incoming WhatsApp keywords, unanswered WhatsApp messages, time spent in a stage, and manually logged CRM calls.

Each rule performs one of nine actions: assign a follow-up sequence, move a lead to a stage, schedule a WhatsApp message, send email, create a call reminder, set an attribute, clear an assigned sequence, set lead AI enabled/disabled, or set assigned Auto Follow-ups enabled/disabled.

Admins can create, edit, delete, enable, disable, and reorder rules. Members can view rules and history. All rule data, metadata choices, and target validation are scoped to the authenticated organization. There are no Smart Trigger plan quotas.

The editor includes search, status filters, empty states, drag ordering and keyboard-accessible order buttons, multiple pipeline groups, stage checkboxes, attribute conditions, personalization chips, a rule summary, and a mobile navigation menu. New rules default to disabled. History shows the latest 100 runs.

## Existing SHVYA integrations

- Pipelines, stages, attributes, attribute types/options, WhatsApp accounts, and sequences are loaded from existing organization-owned models.
- Sequence actions call the existing `assign_sequence`, `clear_sequence`, and `set_lead_followup_enabled` services. Existing sender matching, business hours, sequence progress, and global follow-up controls still apply. Starting a sequence without replacement skips leads with an active or paused assignment.
- CRM call statuses come from `LeadCall.status`. The reference's separate “Missed” status is not invented; the current CRM values are preserved. Calls without a human user are excluded.
- WhatsApp scheduling uses SHVYA's existing Cloud API transport, not the reference product's browser extension. Free text requires an inbound message on that account within the previous 24 hours at send time. Outside that window, the run is blocked; use an approved-template follow-up sequence instead.
- Email goes to the lead's CRM email and uses the existing `FOLLOWUP_EMAIL_DELIVERY_ENABLED` sender-readiness gate. Plain-text composition and `{{attribute_key}}` personalization follow the existing Auto Follow-ups implementation.
- AI actions update the existing `Lead.ai_enabled` field. No lead-level AI toggle UI is added to CRM. Existing organization/stage AI gates remain in effect.
- Auto Follow-ups toggles require an assigned active or paused sequence, matching the current lead-control service.

## Evaluation and timing

Pipeline groups are OR alternatives; stages within a group are OR alternatives. Attribute conditions use AND, with OR among each condition's values. Equals and Contains are case-insensitive. Keywords use case-insensitive substring matching; `*` matches any inbound message. Identical canonical event/condition/action configurations cannot be saved twice, even with different names.

Events capture lead state and the enabled rule IDs at occurrence. New or subsequently enabled rules do not replay old events. Matching rules are evaluated in their stored order before their action snapshots execute. This makes matching independent of mutations performed by an earlier action in the same event. Events from those mutations enter the next dispatcher pass.

Each rule has a 30-second cooldown per lead. Causal rule IDs also propagate through immediate automation-generated events, preventing a rule from repeatedly running within a stage/sequence loop even when queue delays exceed the cooldown.

Stage dwell timers fire once per entry. Actual CRM stage saves update the dwell clock, including legacy callers that save only `stage`. Unanswered-message clocks are created at the first successful outbound WhatsApp send, not at queue creation; delivery and read receipts do not reset them. A new outbound message replaces the prior clock. Replies cancel pending no-response actions. Stage exits cancel pending dwell actions. Timers are rechecked before execution.

Relative message schedules use the run creation time. Fixed schedules select the next occurrence in the organization's timezone. Date-time attribute schedules interpret naive values in that timezone and reject missing or past values. Reminder delays use minutes, hours, or days.

The ten-second dispatcher cadence adds a small processing delay. Timer scanning starts from existing stage-entry timestamps and new successful-send clocks; historical outbound messages are not backfilled.

## Durability and delivery

`TriggerEvent` is a database outbox. Existing CRM/import writers use model saves; their signals persist events without network calls. Callers that introduce `bulk_create` or `QuerySet.update` for lead creation/movement must explicitly emit equivalent events, because Django does not send save signals for those operations.

The dispatcher uses a PostgreSQL advisory lock to avoid overlapping passes. Evaluation and action mutations lock the lead row. Unique event keys and a unique rule/event constraint prevent duplicate work. CRM mutations and new events roll back together inside action transactions. Rule disabling cancels actions that have not been handed to a transport.

WhatsApp creates one queued message row per run and uses the existing sending task. Provider delivery status is reflected in run history. Email uses a durable sending claim and stable Message-ID. An uncertain email outcome is not automatically retried; inspect provider logs for `needs_review` or an interrupted `sending` run before taking manual action.

Statuses distinguish pending, scheduled, queued, completed, skipped, blocked, failed, and email delivery review. The history endpoint exposes only the requesting organization's runs.

## Rollout

1. Apply the two `triggers` migrations with the normal `python manage.py migrate` release step.
2. Collect static assets with the normal `python manage.py collectstatic --noinput` release step.
3. Restart the web process and Celery workers, and restart Celery Beat so it loads `apps.triggers.tasks.dispatch_smart_triggers` every ten seconds.
4. Confirm the existing Redis broker, WhatsApp sender connections, and optional email sender configuration are ready.
5. Create a disabled rule, review its conditions, then enable it and test with a sample lead. Inspect Run history before enabling additional rules.

No production data migration, historical trigger backfill, or new third-party dependency is required. A small existing follow-up service fix hydrates newly created business-hours defaults before first-time sequence assignment.

## Validation

The feature suite covers tenant isolation, admin/member permissions, CSRF, duplicate rules, event idempotency, ordering, cooldowns, causal loops, historical event exclusion, real CRM actions, follow-up integration, timing cancellation, successful-send clocks, keyword matching, scheduling/timezones, and message/email delivery guards.

Local verification ran 41 feature and related regression tests on an isolated PostgreSQL 18 instance. The local-only harness uses in-memory cache/channel layers and adapts the unrelated AI embedding column because the Windows installation lacks pgvector. Those harness settings are outside the repository and are not part of deployment. Repository CI uses its normal PostgreSQL/pgvector and Redis services.

The browser review covered create/save/reopen, scheduling field changes, personalization persistence, run-history empty state, and desktop/mobile layout and navigation.
