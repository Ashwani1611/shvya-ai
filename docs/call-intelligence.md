# SHVYA Call Intelligence

SHVYA Call Intelligence is the Android-first internal employee calling layer.
It is intentionally distributed outside Google Play and uses Android SIM/call-log
permissions including `READ_CALL_LOG`. iPhone support is out of scope for this
release.

## Runtime flow

Android SIM call -> native phone-state detection -> call-log reconciliation ->
Room/outbox -> authenticated SHVYA JWT API -> tenant-safe CallRecord -> CRM Lead
-> canonical LeadCall/timeline -> post-call notes/disposition/reminder ->
grounded Call Intelligence -> validated SHVYA Workflows.

Cloud telephony providers can feed the same backend contract with
`source=cloud`, provider IDs, recording URLs and transcripts. Android SIM and
cloud-provider calls therefore converge on one SHVYA call model.

The server never trusts an organization ID supplied by Android or a provider
client. Organization and employee identity come from the authenticated SHVYA
user, and device ownership cannot silently move between organizations/users.

## API

Production base:
`https://dashboard.shvya-ai.com/api/v1/call-intelligence/`

- `POST devices/register/`
- `POST devices/heartbeat/`
- `POST events/`
- `GET calls/`
- `PATCH calls/<call_id>/media/`
- `PATCH calls/<call_id>/notes/`
- `POST calls/<call_id>/follow-up/`
- `GET analytics/`
- `GET|POST dispositions/`
- `GET settings/`

Every Android event contains a stable `event_uuid`, `device_id`, and
`source_call_id`. The server enforces event idempotency and underlying-call
idempotency. Room persists the call before sync; WorkManager retries transient
failures and JWT access tokens are refreshed through SHVYA's existing refresh
endpoint.

## CRM and AI behavior

Phone numbers are normalized before matching. Matching is restricted to the
authenticated organization. Configured answered, missed, rejected and optional
unknown call types can create a new CRM lead through the canonical CRM lead
service, with source `phone_call`, configured pipeline/stage/owner, and the
existing `LeadCall` timeline.

Call notes or a provider transcript can trigger SHVYA's existing metered AI
provider to create grounded summary, intent, sentiment, objections, buying
signals, competitor, budget, timeline, product interest, decision-maker signal,
next action, follow-up suggestion, AI/qualification scores, coaching metrics,
compliance flags and evidence-backed CRM attribute candidates.

AI analysis never directly mutates a CRM stage or attribute. When analysis is
stored it emits the `call_intelligence_ready` workflow event. Organization
admins can configure intent/minimum-AI-score conditions and reuse SHVYA's
existing validated workflow actions for stage changes, attributes, reminders,
WhatsApp/email and other actions.

## 23-point implementation contract

1. **Universal capture sources** — Android SIM is live; cloud-provider events use
   the same backend model/API.
2. **Offline-first Android** — Room stores calls locally before network sync.
3. **Durable synchronization** — outbox, retry counts, WorkManager constraints,
   JWT refresh and server idempotency.
4. **Universal call model** — organization, user, lead, source/provider IDs,
   phone, status, timing, notes, disposition, media and intelligence fields.
5. **Full event history** — call lifecycle, reconciliation, recording,
   transcription and AI-analysis events are durable.
6. **Tenant-safe lead matching** — E.164-style normalization and organization
   isolation before matching.
7. **Configurable auto lead creation** — answered incoming/outgoing, missed,
   rejected and optional unknown calls with default pipeline/stage/owner.
8. **CRM enrichment** — every matched/created call becomes canonical CRM call
   activity and can drive follow-up/intelligence.
9. **Call Intelligence** — summary, intent, sentiment, outcome, objections,
   buying signals, budget/timeline/product/next action and scores.
10. **Recording/transcript readiness** — provider recording URL, transcript,
    status and speaker-segment contracts are supported.
11. **Backend-owned AI execution** — AI proposes evidence-backed information;
    CRM mutations remain validated backend/workflow actions.
12. **Workflow integration** — `call_logged` and
    `call_intelligence_ready` triggers feed existing SHVYA actions.
13. **Post-call assistant** — Android notifies after a captured call; the
    Call Intelligence workspace supports notes, disposition, follow-up and AI.
14. **Custom call dispositions** — organization-specific connected/not-connected
    outcomes with admin configuration.
15. **Canonical reminders** — reuses `LeadReminder` and its one-reminder-per-lead
    replacement contract.
16. **Missed-call intelligence** — dedicated recent missed-call recovery queue.
17. **Call dashboard** — live metrics, filters, recent calls, transcript/media
    and detailed CRM/intelligence views.
18. **Agent intelligence** — calls, answered/missed counts, average talk time,
    high-intent counts and AI coaching metrics.
19. **Native Android companion** — Kotlin phone-state/call-log capture, Room,
    WorkManager, boot recovery, permissions and battery handling.
20. **Modern background reliability** — foreground tracking plus WorkManager
    reconciliation/sync rather than relying on a single permanent process.
21. **Internal distribution** — no Google Play dependency; `READ_CALL_LOG` is
    intentionally used for employee devices.
22. **Android-only scope** — no iPhone implementation in this release.
23. **Unified architecture/audit** — source -> ingestion -> idempotent call ->
    lead/timeline/media -> intelligence -> validated action -> durable event
    history.

## Dashboard and Android distribution

The shared SHVYA sidebar exposes **CALL INTELLIGENCE** at
`/dashboard/call-intelligence/`. The page contains the product feature
showcase, Android APK download, call operations, filters, missed-call queue,
device health, agent intelligence, post-call assistant controls and
organization settings.

The APK is built by the dedicated Android GitHub Actions workflow. A successful
`main` build publishes the stable internal release tag
`call-intelligence-latest`. The authenticated dashboard download endpoint
redirects to that release by default; `CALL_INTELLIGENCE_APK_URL` can override
the location if distribution moves to private object storage.


## Overview / Analytics and Android 1.0

The sidebar entry opens Overview. Analytics is selected with
`?section=analytics` and contains Calls at a glance, the full paginated call
history, filters, missed calls and team metrics. Every web metric uses the
same filtered queryset. Clear resets all filters while staying in Analytics.
Existing filter URLs still select Analytics.

Overview contains product information, APK download/setup, connected phones
and organization configuration. Removing a phone is a CSRF-protected POST,
scoped to the current organization and either the device owner or an admin.
The device is deactivated rather than deleted, preserving its call history.
Registration and new ingestion reject deactivated devices with
`403 / device_removed`, so periodic workers cannot silently reconnect them.
The mobile client stops background work and clears its local session on that
response. A removed device must be explicitly re-enabled by an administrator
before it can register again.

Ringing is a duration in seconds, not an estimated count of audible rings.
The current Android receiver measures incoming ringing when lifecycle events
are available. Missing / legacy zero timings display as unavailable and do
not enter ring averages. Outgoing audible-ring counts cannot be derived from
Android call-log duration and are never invented.

The native app uses SHVYA branding, an adaptive launcher icon, permissions
onboarding, Home / Reminders navigation, call search and dates, real call
statistics, paginated call history, manual lead creation, assigned reminders
and organization settings. Reminder completion, snooze and deletion update
the canonical CRM reminder. Snoozing an overdue reminder sets it at least
30 minutes into the future. Agents can view lead-creation settings; only
organization admins can change them. A settings PATCH changes only supplied
boolean fields, preserving other pipeline / owner / automation settings.

Additional mobile API endpoints:

- `GET reminders/` (current employee assignments; paginated)
- `POST reminders/<uuid>/action/` (`complete`, `snooze`, `delete`)
- `GET leads/` (organization-scoped pipelines, active stages and every custom
  attribute's name, type and options; descriptions are omitted)
- `POST leads/` (name, phone, email, notes, selected pipeline/stage and custom
  attributes; existing leads are never overwritten)
- `PATCH settings/` (admin-only lead-creation booleans)
- `GET calls/?mine=1&page=1` includes filtered totals and `has_next`

SHVYA vector artwork source:
https://kraya-ai.com/images/Assets-SHVYA-Homepage/kraya-blue-logo.svg
The existing Android package identifier remains unchanged. The displayed
version is 1.0 (code 3, which is higher than the previously shipped code so
Android can update installed devices). CI remains the canonical signing/distribution route; a locally
built APK uses a local debug signing key and is for review only.

### Review builds and validation

The debug variant uses `com.shvya.callintelligence.preview` and the launcher
label “SHVYA Call Intelligence Preview”, so it can be installed alongside the
existing app. The release application ID is unchanged. Deploy the matching
backend endpoints before using the new mobile screens against a live account.

Local validation: 25 telephony tests passed using temporary SQLite settings
with PostgreSQL GIN index creation omitted, plus Ruff and whitespace checks.
This verifies application behavior, not PostgreSQL migrations or concurrency.
The dashboard was inspected at desktop and 390px mobile widths during this
change. Physical-device permission, incoming-call timing and background-sync
checks remain part of release validation.

Both `assembleRelease` and `assembleDebug` completed successfully locally.
The preview APK's package/label, adaptive launcher icon and v1/v2 signatures
were verified with Android build tools.
