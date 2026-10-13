# Call Intelligence

> **Current-reference refresh:** verified on 2026-10-12 against `main` commit `24f2a1d5f75d`. Source code, Django models/migrations, tests, and runtime configuration remain the executable source of truth.

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
- The currently published dashboard download is **v1.6 Preview** (version code 10), package `com.shvya.callintelligence.preview`. It installs alongside the prior app because the original signing key was unavailable; it is not a mandatory in-place update. Publication verifies the checked-in and publicly served APK SHA-256 before activating the release record. See [`android-release-publication.md`](./android-release-publication.md).

Pipeline, stage and attribute validation remain backend-owned and tenant scoped.

## Analytics and employee routing

- Call source means the capture channel, not the lead acquisition source. Android SIM events are synced by the APK. Manual CRM `Mark call` entries create a linked CallRecord and queue analysis when notes are present. Historical completed/no-response/busy CRM calls without a CallRecord are backfilled in bounded batches by the Call Intelligence recovery task. Scheduled/cancelled entries are not counted as completed call activity. Cloud events use the authenticated generic event API; this does not imply a named provider is installed.
- New APK leads route to the authenticated employee's active owned pipeline. Agents without a pipeline receive an actionable validation error; their leads are never silently routed to another employee. Admins without an owned pipeline retain organization fallback settings. Mobile manual creation uses the same routing and limits available choices to owned pipelines when assigned.
- Existing leads are matched organization-wide by normalized phone. Calls attach to the existing lead and canonical LeadCall, retaining its pipeline/stage regardless of which employee made the call. Repeated events do not duplicate CRM calls.
- Creating a lead or editing its phone reconnects earlier unmatched calls after the CRM transaction commits, including national trunk and international dialer prefixes. Dashboard and mobile analytics also repair legacy exact-phone links before filtering/counting. Pending calls attach to the lead immediately; terminal calls create one canonical CRM call. Late ringing/started events cannot replace a completed outcome.
- Duration labels use hours/minutes/seconds, while APIs preserve raw seconds and provide formatted duration strings. Talk averages and totals include answered calls only. Ring averages exclude unavailable zero measurements.
- Dashboard outcome, source, pipeline, employee, status, intent, search and date filters apply to all cards and team rows. Answer rate is answered/all calls; conversion rate is converted dispositions/answered calls. Conversion is a call outcome, not a unique lead or revenue metric. Follow-ups due next 24 hours exclude overdue calls, which have a separate count.
- Missing standard dispositions are seeded for existing organizations without overwriting custom or disabled choices. Connected and not-connected outcomes are grouped in the call form. AI scores show only analyzed calls; absent analysis is not treated as a zero score.

## Latest calls workspace

- Recent Activity uses clickable outcome tabs with counts across the complete matching history, including Not classified and historical inactive outcomes. Selecting an outcome resets pagination and preserves search, employee, source, date and CRM filters. The separate All contacts, CRM leads and Needs a lead segments narrow the same history.
- Each call shows its logging user, source, outcome, Intelligence state and current CRM pipeline/stage before opening details. Expand a call for notes, analysis and follow-ups. View lead in CRM opens the matching pipeline, stage and lead only when the viewer has CRM access. All calls for this contact uses the linked lead ID, so historical phone-format changes do not split its history.
- Unknown contacts have a Create lead action with name, read-only call phone, optional email, pipeline and stage. Admins may choose an active organization pipeline; agents may choose only active pipelines they own. Stage choices belong to the selected pipeline, and the backend validates both permissions and stage membership.
- Creation uses the canonical CRM service without sending a welcome message. An existing organization/phone lead is reused without changing its name, pipeline or stage. The original call is linked once with its original user, duration and notes; earlier matching calls reconnect through the normal CRM reconciliation path. The workspace then opens the original call within that contact's complete history.
- Saving a changed outcome refreshes the selected group and its counts. Saving notes with the same outcome retains the editor and continues the existing asynchronous Intelligence refresh.

## Post-call analysis lifecycle

- Notes, provider transcripts and manual CRM calls use one analysis request service. Requests publish after commit, and a persisted evidence hash prevents duplicate analysis and results from overwriting newer notes. Saving new evidence invalidates the earlier result; clearing all evidence removes it.
- CallRecord persists `analysis_status`, a public-safe `analysis_error`, the evidence hash, attempt count and status timestamp. API call responses expose `analysis_status` and `analysis_error` alongside the existing Intelligence fields. Legacy results remain visible as completed.
- The dashboard polls its session-authenticated `calls/<call_id>/status/` endpoint and refreshes the Intelligence badge/details automatically without replacing the notes being edited. Failed analysis displays an actionable error; Save & analyze retries it.
- Analysis uses the existing OpenAI provider and organization credit accounting. The Call Intelligence output budget defaults to 2,000 tokens for its full structured result. Existing OpenAI configuration and AI credits are required; no additional credentials are introduced.
- `apps.telephony.tasks.recover_call_intelligence` runs every minute on the general worker, with batches capped at 100. It repairs historical tracking, queues legacy unanalyzed evidence, and republishes queued/processing jobs stale for five minutes. Provider attempts are capped at three. Broker interruptions preserve saved notes and queued state for recovery.
- Apply the telephony analysis-state migration and restart web, the general worker and Beat with the same release. Recovery does not transcribe an audio-only recording; notes or a supplied transcript remain necessary evidence.


## Production domain migration compatibility

Previously installed APKs may still have `https://dashboard.shvya-ai.com/` compiled
as their API origin. Production Nginx therefore proxies legacy `/api/` requests
directly to Django instead of redirecting them. This preserves POST methods and JSON
request bodies for sign-in, token refresh, device registration and call sync while
normal browser traffic on the legacy hostname continues to redirect to
`https://shvya-ai.com/`.

## Android session and sync recovery (v1.6)

- Sync, Reminders and Refresh retain the signed-in workspace when a request fails.
  A disconnected device pauses call capture/upload and displays an explicit
  reconnect message. It does not delete the login. Device revocation remains
  enforced by the server; reconnect only succeeds after an administrator restores
  access.
- Access-token refresh is serialized across foreground requests and background
  workers. Waiting requests reuse the rotated pair. Late responses from an earlier
  login cannot overwrite the current session. Network errors, throttling, server
  failures and malformed token responses preserve the stored session; a rejected
  refresh token prompts the user to sign in again.
- Local call queues and tracking checkpoints are isolated by account. On upgrade,
  the existing signed-in account retains its legacy queue. A legacy queue with no
  identifiable signed-in owner is preserved but not uploaded by a later account.
- Registration, event upload and heartbeat return the same explicit
  `403 device_removed` response for a revoked device belonging to the caller.
  Unknown devices and devices belonging to another user remain undisclosed.
- The native workspace uses a consistent type scale, contact avatars, quick call
  actions, reminder segments, explicit sync status, and responsive metric cards.
  Returning from a dialer or permission screen no longer needlessly rebuilds an
  open form.

Android verification runs with:

```bash
gradle -p android/call-intelligence testReleaseUnitTest lintRelease assembleRelease
pytest apps/telephony/tests/test_mobile_authentication.py
```

The APK workflow saves the APK, checksum, unit-test results and lint report.
Before distributing to existing devices, verify the signing certificate matches
the installed build. Physical-device acceptance should include Sync, Reminders,
Refresh, an expired token, a temporary loss of connectivity, return from the
dialer, and administrator disconnection/reconnection. Emulator/build results do
not establish real SIM capture or notification delivery on every device vendor.
