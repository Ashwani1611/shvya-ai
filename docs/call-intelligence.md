# SHVYA Call Intelligence

SHVYA Call Intelligence is the Android-only internal employee calling layer.

## Flow

Android SIM call -> native call-log reconciliation -> Room/outbox -> authenticated
SHVYA JWT API -> tenant-safe CallRecord -> CRM Lead -> existing LeadCall/timeline ->
existing Workflows -> notes/reminders -> AI call intelligence.

The server never trusts an organization ID supplied by Android. Organization and
employee identity always come from the authenticated JWT user.

## API

Production base:
`https://dashboard.shvya-ai.com/api/v1/call-intelligence/`

- `POST devices/register/`
- `POST devices/heartbeat/`
- `POST events/`
- `GET calls/`
- `PATCH calls/<call_id>/notes/`
- `POST calls/<call_id>/follow-up/`
- `GET settings/`

Every device event contains a stable `event_uuid`, `device_id`, and
`source_call_id`. The server enforces event and underlying-call idempotency.

## CRM behavior

Phone numbers are normalized to E.164. Matching is limited to the authenticated
organization. Configured answered, missed and rejected call types can create a
new CRM lead through the canonical CRM lead service, with source
`phone_call`, and attach the existing `LeadCall` timeline model.

Call notes can trigger SHVYA's existing metered AI provider to create an advisory
summary, intent, sentiment, objections, buying signals, competitor, budget,
timeline, next action and evidence-backed attribute candidates. AI analysis does
not directly mutate CRM stages or attributes.

## Android distribution

The dashboard page `/dashboard/call-intelligence/` exposes the authenticated
internal APK download. By default it points at the stable GitHub release tag
`call-intelligence-latest`. Override with `CALL_INTELLIGENCE_APK_URL` if the
APK moves to private object storage.
