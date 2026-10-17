# Superadmin Message Activity

Open **Superadmin → Organisations → select an organisation → Message Activity**.
The organisation page links to the workspace with its selected dates. The
organisation tools sidebar also links to it.

## What the workspace shows

The workspace reads SHVYA's existing domain records. It does not send messages,
run AI, retry a job, change a lead, or start an automation.

| Activity | Authoritative source | Meaning |
| --- | --- | --- |
| WhatsApp message | `WhatsAppMessage` | Inbound and outbound messages from API, Coexistence and Hosted connections, including template and campaign sends |
| Instagram message | `InstagramMessage` and its conversation | Direct messages, including conversations with no phone number or CRM lead |
| AI execution | `AITrace` | Started, processing, completed, blocked, failed, stale, duplicate or silenced AI runs; model, retained preview and saved outcome links |
| AI queue | `HostedAutomationJob`; inbound `shvya_ai_execution` state for API/Instagram | Durable queued, processing, retrying, failed or finished work; its saved attempt count and scheduling information |
| Cadence attempt | `FollowupExecution` | Pending, processing, sent, retry-wait, skipped, blocked and failed sequence attempts, plus email actions and CRM reminders |
| Current Cadence progress | Active/paused `LeadSequenceState` | Present sequence assignment and next scheduled step; individual attempts remain separate records |
| Workflow execution | `TriggerRun` with its rule/event/lead | Recorded Workflow actions and results, including actions blocked before a message exists |

Each record shows its recorded time, lead and contact details, current pipeline,
stage and owner, linked connection, type/origin, status, sequence or campaign
where available, message content and safe reason/error. **Inspect** opens the stored content,
timestamps, processing milestones and exact related records. Canonical messages
also link to the existing **API** explorer, scoped to this organisation and the
message ID, using its supported 90-day view.

### Sender and status interpretation

- A message's `from_number`/`to_number`, or Instagram sender/recipient IDs, come
  from the saved message. They are not replaced by the lead's current pipeline
  number or by the connection's current display name. Historical disconnected
  connections remain available in the selector while their records exist.
- A Meta `phone_number_id` is an API identifier, not a telephone number.
  WhatsApp connection labels use the actual display number. Hosted number
  fallback follows its existing canonical number convention.
- Coexistence uses the API transport. The connection label uses SHVYA's existing
  successful onboarding/synchronisation evidence. The historical account enum
  value `hosted` denotes Hosted linked-device sessions, not Coexistence.
- **AI completed** or **queue completed** describes processing. The linked
  message owns its sent/delivered/read/failed status. A sent/provider-accepted
  message is not automatically delivered.
- A saved update time is shown as a record observation, never invented as the
  provider's delivered/read timestamp. Exact campaign receipt times are shown
  when retained on the associated `CampaignAttempt`.
- Queued outbound AI delivery shows its retained next eligible time and reason.
  Current sender pacing is labelled as current state, not a historical receipt.

## Filters and pagination

Filters include inclusive **From/To dates**, current pipeline, linked
WhatsApp/Instagram connection, pipeline owner, sequence, activity type, outcome,
direction, and search. Search covers names, phone/email, stored message text,
provider IDs and relevant recorded reasons. The initial view covers the last
seven local calendar dates. A selected range can contain at most 367 dates.

The organisation timezone defines day boundaries and displayed dates. Message
records use `created_at`; AI runs use `started_at`; attempt/job records use their
recorded creation time. Current Cadence progress uses its last update time.
Scheduled, sent, processing and completion timestamps remain separate columns.

Pipeline, stage and owner are the **current CRM assignment**. Any valid AI trace
snapshot is identified separately in history. A move between pipelines does not
rewrite the original message's sender. A pipeline filter excludes messages with
no linked CRM lead; the connection filter still includes them.

The page contains 50 activity records and has deterministic descending
time/type/ID ordering. Summary cards explicitly distinguish the total matching
records from counts on the current page. A message and its AI/Cadence execution
are different records, so total activities must not be reported as total
messages. Invalid or foreign filter IDs return a helpful HTTP 400 without
falling back to a broader result set.

## Excel export

**Export Excel** downloads a genuine `.xlsx` workbook using every applied filter,
independent of the visible page. Up to 50,000 matching records can be exported
per request. A larger result returns HTTP 422 and asks for narrower filters;
there is no silent row truncation. The export also rechecks the limit while
writing to protect against records arriving after the initial count.
If an auxiliary history, links or long-content sheet reaches Excel's physical
row limit, the same HTTP 422 response asks for narrower filters. No partial
workbook is returned.

The workbook contains:

- **Message Activity:** messages and automation records, routing, content,
  reasons, IDs, models and the separately recorded timestamps.
- **Recorded History:** the available milestones exported with each record.
- **Linked Records:** validated activity-to-activity references.
- **Export Details:** organisation, generation time, timezone, filters,
  exported count, field meanings and evidence limits.
- **Long Content**, only when necessary: ordered parts preserving text that
  exceeds the safe Excel cell length. Join the parts in order to recover the
  entire exported field.

Phone numbers and IDs are written as actual Excel strings. A phone such as
`+91 81691 95976` exports as `918169195976`, without scientific notation or an
Excel precision loss. No country code is guessed. Message text beginning with
`=`, `+`, `-` or `@` remains inert text. Date columns are actual Excel dates in
the timezone named in Export Details. Invalid XML control characters are
removed so a malformed source message cannot break the download.

The workbook uses streamed row writing and a temporary response file. The file
and intermediate XML streams are closed/removed on success and handled failure.
No exported customer content is copied into the repository or public media.

## Evidence and access boundaries

This is a view of **retained evidence**. Deleted messages, expired API
observations, unrecorded receipts and historical scheduling changes cannot be
reconstructed. AI trace content may be a stored preview; the UI and export label
it accordingly. Detail pages show at most the latest 100 related records per
source and latest 100 matching API observations; the canonical activity feed
remains paginated across all matching retained records.

Campaign sends appear through their canonical WhatsApp messages and related
attempt evidence. A campaign recipient cancelled/skipped before any message was
created remains in the existing Campaigns workspace. Standalone email histories
outside recorded Cadence/Workflow actions are not added by this feature.

Every entry point requires the dedicated Superadmin session and a platform
superuser. Reads validate the organisation on accounts, leads, current pipeline
and stage, sequence/state/step relationships, Workflow rules/events, and
cross-record references. Operational test fixtures are excluded. Foreign or
malformed detail IDs fail closed. Stored content is escaped; raw provider
payloads, encrypted connection credentials, prompts and internal AI reasoning
are not rendered or exported. Responses are private/no-store and noindex.

## Implementation and release

- Service: `apps/superadmin/message_activity.py`.
- HTTP views: `apps/superadmin/message_activity_views.py`.
- XLSX writer: `apps/superadmin/message_activity_export.py`.
- Templates: `templates/superadmin/message_activity*.html`.
- Styling: `static/superadmin/message-activity.css`.
- Tests: `apps/superadmin/tests/test_message_activity.py` and
  `apps/superadmin/tests/test_message_activity_export.py`.

The service unions small indexed record identities in SQL and hydrates bounded
batches. It does not build a second message ledger or scan provider APIs. No
schema or dependency change is required. Normal deployment collects the new
static CSS and restarts the application on the verified commit.

Run the focused tests, the existing organisation/AI Brain regressions, Ruff,
Django system checks and migration drift checks. Native PostgreSQL CI and
Security must pass for the PR and merged main revision before the existing
production deployment workflows run. A local PGlite run is supplementary
PostgreSQL-compatible evidence, not a replacement for native PostgreSQL 17 CI.
