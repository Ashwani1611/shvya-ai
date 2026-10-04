# Call Intelligence

> **Implementation snapshot:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. Source code, Django models/migrations, tests, and runtime configuration remain the executable source of truth.

Call Intelligence links Android SIM call activity and future compatible call sources to SHVYA CRM without making the phone device the system of record.

## Components

- Django domain: `apps/telephony/`
- Dashboard: `/dashboard/call-intelligence/`
- API: `/api/v1/call-intelligence/` and the versioned `/api/v1/telephony/` include
- Android companion: `android/call-intelligence/`
- CRM linkage: `Lead` + canonical `LeadCall`

## Data model

Current staging models are:

- `CallIntelligenceSettings` — organization enablement, auto-create rules and default pipeline/stage/owner.
- `CallDisposition` — organization disposition configuration.
- `CallDevice` — Android device/user ownership, permissions, app/device metadata and heartbeat state.
- `CallRecord` — canonical tenant-scoped call identity, timings, phone/contact data, CRM linkage, recording/transcript and follow-up state.
- `CallEvent` — durable call lifecycle events.
- `CallIntelligenceResult` — summary, intent, sentiment, outcome, objections, buying signals, budget/timeline/next action, scores and extracted analysis.
- `CallAppRelease` — Android release/download metadata.

`CallRecord` is unique by `(organization, source, source_call_id)` so retries/reconciliation do not create duplicate canonical call rows.

## Android SIM flow

1. An authenticated organization user registers the Android device.
2. The app maintains heartbeat/permission state.
3. Android call-log/state events are normalized and posted to the Call Intelligence API.
4. Django validates device ownership and organization scope.
5. The service resolves or creates CRM linkage according to organization Call Intelligence settings.
6. The canonical `LeadCall` / activity evidence is updated through SHVYA service logic.
7. Recording, transcript, notes, disposition and follow-up information can be attached to the canonical call.
8. Analysis fields remain derived evidence; CRM permission/stage rules continue to be backend-owned.

The Android client is currently an internal Android implementation. Documentation must not assume iOS support.

## API routes

Below the Call Intelligence API prefix:

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

## Dashboard operations

The web workspace includes call list/detail actions, settings/dispositions and the Android download entry. Tenant access is enforced server-side.

## Safety and integrity

- A device cannot be silently rebound across users/organizations.
- Default pipeline/stage/owner settings must belong to the same organization.
- Call identity is idempotent by tenant/source/source-call ID.
- Lead and CRM-call relationships remain organization scoped.
- Recordings/transcripts can contain sensitive customer data and must not be exposed through unrelated diagnostics or external-AI tools.
- AI-derived call analysis is not authority to bypass CRM qualification, stage requirements or consent/policy controls.

## Android CRM lead form

The current Android Call Intelligence app can load organization-scoped CRM
lead-creation configuration instead of hard-coding pipeline or custom-field
choices.

Additional mobile API behavior:

- `GET /api/v1/call-intelligence/leads/` returns organization-scoped pipelines,
  active stages and custom-attribute metadata (name, type and options; internal
  descriptions are not exposed).
- `POST /api/v1/call-intelligence/leads/` accepts name, phone, email, notes,
  the selected pipeline/stage and validated custom attributes. Existing leads
  are not overwritten by this creation path.
- The Android app displays version 1.0 with version code 4 so installed internal
  builds can upgrade from earlier lower version codes.

Pipeline, stage and attribute validation remain backend-owned and tenant scoped.

## Analytics and employee routing

- Call source means the capture channel, not the lead acquisition source. Android SIM events are synced by the APK. New manual CRM `Mark call` entries now create a linked CallRecord. Historical CRM calls without a CallRecord are not automatically backfilled. Cloud events use the authenticated generic event API; this does not imply a named provider is installed.
- New APK leads route to the authenticated employee's active owned pipeline. Agents without a pipeline receive an actionable validation error; their leads are never silently routed to another employee. Admins without an owned pipeline retain organization fallback settings. Mobile manual creation uses the same routing and limits available choices to owned pipelines when assigned.
- Existing leads are matched organization-wide by normalized phone. Calls attach to the existing lead and canonical LeadCall, retaining its pipeline/stage regardless of which employee made the call. Repeated events do not duplicate CRM calls.
- Duration labels use hours/minutes/seconds, while APIs preserve raw seconds and provide formatted duration strings. Talk averages and totals include answered calls only. Ring averages exclude unavailable zero measurements.
- Dashboard outcome, source, pipeline, employee, status, intent, search and date filters apply to all cards and team rows. Answer rate is answered/all calls; conversion rate is converted dispositions/answered calls. Conversion is a call outcome, not a unique lead or revenue metric. Follow-ups due next 24 hours exclude overdue calls, which have a separate count.
- Missing standard dispositions are seeded for existing organizations without overwriting custom or disabled choices. Connected and not-connected outcomes are grouped in the call form. AI scores show only analyzed calls; absent analysis is not treated as a zero score.
