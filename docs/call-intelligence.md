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
