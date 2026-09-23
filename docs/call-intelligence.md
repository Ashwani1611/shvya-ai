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
- The Android app displays version 1.0 with version code 3 so installed internal
  builds can upgrade from earlier lower version codes.

Pipeline, stage and attribute validation remain backend-owned and tenant scoped.
