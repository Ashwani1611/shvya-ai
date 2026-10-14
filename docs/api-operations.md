# Superadmin API Operations Center

Open **Superadmin → API** at `/superadmin/api/`. The existing dedicated
Superadmin session and `superuser_required` boundary protect all HTML and JSON
views. Normal CRM users and anonymous requests cannot access this global view.

The explorer supports organization, integration, outcome, time range and ID/error
search. Open an activity for its recorded steps, diagnostic evidence, related
requests/worker attempts and callback observations. Related records match a
correlation ID, or an exact entity/provider reference within the same organization.
This view does not send messages, replay webhooks or alter provider state.
Superadmin can separately manage operator investigation state through CSRF-protected
POST actions; evidence GET routes remain read only.

The four overview counts are links. **Needs attention** opens failed, blocked,
partial, unknown and explicitly overdue records; **Waiting for next step** opens
only active work with an expected deadline. Counts retain the time, organization,
integration, type and search scope when switching outcome filters. The latest
attention records show the organization, integration, operation or endpoint,
error code and a direct **Inspect evidence** link. Organization names link to
that organization's attention items without dropping the search filter. The activity detail shows its last recorded
step and a return link to the filtered explorer. An activity ID is never shown
as if it were an organization ID. An unattributed record retains that label;
an unverified organization reference is displayed separately. The explicit
`organization=unattributed` selection filters only records with no resolved tenant;
it is supported by the explorer, health endpoint and investigation inbox.

## Journeys and investigations

Activity detail now includes a bounded journey of up to 100 activities. Explicit
parent IDs and shared correlation are labeled separately from shared domain or
provider references. All associations preserve organization attribution; platform
records require the same reported organization reference. A shared domain reference
does not establish causality. Truncation and missing parents are shown explicitly;
this is not a guarantee that every processing step was captured.

Open an investigation from an activity that needs attention. Assign an active
Superadmin, acknowledge it, choose severity, set a review deadline, snooze, add an
operational note, resolve or reopen the review. Each change records its actor and
bounded, redacted audit data. Version checks and activity row locks reject stale
edits. Resolving the investigation never rewrites the original API or message
outcome. The `/superadmin/api/incidents/` inbox shows active, owned, unassigned,
resolved and overdue reviews, with in-app deadline notices. Reviews begin with a
24-hour deadline; deadline and snooze extensions are limited to 1–168 hours.
There are no external alert notifications. The scheduled review-discovery task opens incidents for retained unreviewed attention activities every five minutes when activity capture is enabled; it never resends customer operations.

The saved-outcome check reads only the matching organization's and connection's
canonical WhatsApp, Instagram or outgoing-webhook state. It reports receiver
acknowledgement separately from delivery or downstream completion. A provider-ID
mismatch or missing attribution/object is not silently treated as success. The
check does not call provider APIs, resend, poll history or automatically resolve
uncertain outcomes. Recording a check adds a sanitized snapshot to the review audit.

Open and acknowledged investigations pin their activity, timeline and review audit
beyond normal retention. Snoozing does not remove that protection. Once resolved,
the evidence again follows the original activity retention date and may be pruned
on the next cleanup run. Monitor unresolved review volume: this preservation can
increase storage. Cleanup locks the same activity rows as investigation writes so
an operator cannot lose a newly pinned record during concurrent pruning.

Durable delivery handoff and provider retries remain owned by the existing channel
and integration services. This release does not introduce universal replay,
provider reconciliation or a generic outbox for every task. A safe replay adapter
must first verify current routing, provider outcome, duplicate protection and the
specific domain's retry contract.


## Organisation health and operator queue (October 2026)

The Superadmin API homepage now includes an organisation health table, with
recorded/completed/attention/unknown activity counts and open review counts.
Health and its evidence links use the same time, organisation, integration,
activity-type and search filters as the explorer. Like overview cards, health
compares all outcomes independently of the outcome tab. The **Inspect attention**
links preserve this scope and reset pagination. Attention includes overdue open
work; the JSON compatibility field `failures` remains issue-status-only, with
`attention` and `overdue` returned separately. Unknown is a subset of attention.
This is not an uptime SLA or an end-to-end message-delivery report.

Read-only health data is available at `/superadmin/api/health/data/` for
 authenticated Superadmins. Supported `hours` values are `1`, `24`, `168`, `720`,
and `2160`; equivalent `range` values are `1h`, `24h`, `7d`, `30d`, and `90d`.
Conflicting `hours` and `range` values return HTTP 400. A 90-day selection uses
2160 hours in both health and explorer, not a silently reduced 30-day window.
`organization` accepts a UUID or `unattributed`. See [health rollout details](api-health-rollout.md).

The investigation inbox groups **open** reviews by organisation, integration,
error category and error code. Each group shows high-severity and overdue counts.
These are symptom cohorts, not deduplicated master incidents or proof of root cause.
An operator attention queue orders active reviews by their review due dates and
links to the evidence page. It is not an outbound notification feed.

Celery Beat schedules `core.discover_api_incidents` every 300 seconds on the
general worker, scanning up to 200 retained attention activities per invocation,
oldest first and without a creation-time cutoff. Old failures and older work
that becomes overdue remain eligible while evidence is retained. Discovery
creates a review record once per activity using row locks and a unique activity
link; it does not reopen an existing resolved review. When capture is disabled,
the scheduled task skips discovery. Operational verification requires confirming
Beat and worker execution, not only a successful deployment workflow.
Run a bounded manual scan using `python manage.py discover_api_incidents --limit 100`.
An optional `--hours 24` limits issue outcomes by their updated time, not creation
time; overdue open work is always included. Existing record/retention bounds
remain in effect. Older unreviewed evidence may create additional review tickets
after rollout; monitor unresolved-review storage and worker capacity.

Not yet supported: external notifications, provider-API reconciliation, guarded
provider retries, automatic root-cause verification, and authoritative
transaction-impact counting. Never infer those abilities from the incident UI.

## Outcomes and evidence

| Outcome | Meaning |
| --- | --- |
| Queued / processing | The recorded step was waiting or executing at that point. Historical message state observations are not a live queue. |
| Accepted | A request was acknowledged or a send accepted. This is not delivery, template approval or CRM completion. |
| Completed | The particular operation completed: for example HTTP read, worker execution, CRM import or an observed delivery receipt. |
| Failed / blocked / partial | The operation recorded this result. The error code and timeline establish the available evidence. |
| Unknown | Evidence is insufficient to confirm the outcome. A timeout during a mutating request can have this result. |
| Skipped | A duplicate, ignored event or policy-deferred attempt was observed. |

Only open activities with an explicit `expected_by` contribute to waiting and
overdue totals. An overdue deadline is a reason to investigate, not proof of
failure. HTTP-response deadlines clear when the response arrives. Scheduled tasks
start their queue deadline after ETA; worker execution receives a separate deadline.
Concurrent worker redeliveries atomically claim the initial queued record once and
create separate subsequent attempts. A completed worker does not establish that
every business action it attempted succeeded; inspect its child records.

Issue groups match recorded symptoms and error codes. Diagnostic guidance points
to checks supported by those classifications; it does not assert an unverified
root cause. A lack of records does not establish integration health.

## Capture boundaries

| Area | Captured evidence |
| --- | --- |
| SHVYA HTTP | Route template, verb, response code, duration, request ID and resolved organization for instrumented API/webhook routes and dashboard/admin mutations. |
| Celery | Publish intent, broker-return event, execution, failure, retry, queue and ETA; request/activity context propagates without task arguments or results. |
| WhatsApp API | Provider send/media/account requests, canonical message-send attempts and saved message-state observations, including numeric callback errors. |
| Coexistence | Capability and asset requests, echo/history/app-state handlers. Ordinary sends use the shared WhatsApp API transport label. |
| Hosted WhatsApp | Django-to-gateway requests, send attempts and saved session/message states. |
| Instagram | Provider requests, sends, saved message states and inbox-envelope processing states. |
| Meta templates | Actual submit/delete overrides, sync/analytics requests and template operation records. |
| Meta Lead Ads | Fetch requests, routing/mapping blocks, duplicate/create/update outcomes and caught processing failures even when the webhook returns HTTP 200. |
| IndiaMART / JustDial | Intake validation and CRM results, duplicate outcomes and failures. IndiaMART failed batch transactions do not retain rolled-back successes. |
| Outgoing webhooks | Each delivery attempt and its transport result, alongside the existing delivery model. |
| AI | Settled usage from the existing credit ledger: model, feature, token counts and credits. Allowlisted operational diagnostics attach to the current request/task timeline. Credits are not a currency estimate. |

Successful dashboard/Superadmin page loads and GET/HEAD/OPTIONS polling are excluded
from this database history to avoid amplifying periodic status reads. Existing
bounded HTTP metrics still cover those requests. Health probes, static assets and
the API Operations Center itself do not generate activity rows.

API-key identity is bound only after verification. Ambient browser cookies do not
attribute provider webhooks or unauthenticated API requests. Shared callback
envelopes can remain platform/unattributed while their domain records carry the
server-resolved organization. Provider callbacks arriving later usually have a
different trace and are linked by domain identifiers instead.

## Storage, privacy and limitations

`core.APIActivity` stores the latest observation and `core.APIActivityEvent` stores
its timeline. Domain models remain authoritative. Fields are bounded and indexed
for time, organization, integration, status and exact related identifiers.

No raw request/response bodies, authorization headers, cookies, API tokens,
message text, QR data or prompts are copied. Outbound URLs retain only the host;
inbound URLs use resolver templates. Provider errors retain allowlisted codes,
subcodes, types and trace IDs rather than arbitrary response text. Additional
metadata is size/depth limited and redacted again when rendered. This is not a
privileged raw-payload viewer.

Capture is synchronous and best effort: database errors are contained with nested
savepoints and content-free capture-failure signals. Database latency can still
delay the calling operation. Activity writes inside a surrounding business
transaction roll back with it. This feature is **not a new durable inbox/outbox**,
nor a guaranteed audit trail. The UI shows capture-disabled/cache-unavailable/write
failure notices; the independent operations logger is the fallback if cache fails.

There is no historical backfill or universal interception of `QuerySet.update`.
Signals observe saved states and may repeat a state; they do not reconstruct every
transition. Hard worker crashes can leave an overdue record. Internal gateway
JavaScript events before a Django callback, provider internals, uninstrumented
third-party clients and unreported AI usage are outside this release. No automatic
reconciliation, alert delivery, safe replay or OpenTelemetry exporter is configured.
Existing `/health/runtime-metrics/` remains the token-protected source for
Redis/worker/gateway runtime snapshots; those infrastructure snapshots are not
duplicated by this page.

## Rollout and retention

1. Apply migrations with `python manage.py migrate` before starting the updated
   web and worker processes. The original core migration only adds tables/indexes;
   the health-scope and discovery fixes require no additional schema migration.
2. Run the deployment's normal static collection and restart web, workers and Beat.
3. Open `/superadmin/api/`; exercise an approved existing workflow and verify its
   new records and correct organization. Capture starts with new traffic.

| Setting | Default | Purpose |
| --- | --- | --- |
| `API_ACTIVITY_ENABLED` | `True` | Stop new capture without removing existing history. |
| `API_ACTIVITY_RETENTION_DAYS` | `30` | Expiry target for activities and their events. |
| `API_ACTIVITY_REQUEST_DEADLINE_SECONDS` | `60` | Incoming HTTP response investigation deadline. |
| `API_ACTIVITY_QUEUE_DEADLINE_SECONDS` | `300` | Worker-start deadline after publication/ETA. |
| `API_ACTIVITY_TASK_DEADLINE_SECONDS` | `900` | Worker-completion investigation deadline. |

Deadlines are monitoring thresholds; they do not cancel requests/tasks or change
provider retry policy. Tune them for the actual workload.

Beat runs `core.prune_api_activities` hourly on the existing general worker. Each
run deletes at most 20 batches of 500 activities, checking a 30-second budget
between batches. These bounds protect worker capacity, not a strict deletion SLA.
A backlog records `api_activity.retention_backlog`; investigate cleanup capacity if
expired volume grows faster than hourly removal. Preview or process it manually:

```bash
python manage.py prune_api_activities --dry-run
python manage.py prune_api_activities --batch-size 500 --max-batches 100
```

Disabling capture does not disable retention. To roll back the runtime feature,
set `API_ACTIVITY_ENABLED=False` and restart web/workers; existing records stay
readable. Do not drop the tables merely to disable capture.

## Adding a new integration

Use `observed_request` at the actual provider transport boundary and retain its
response/exception contract. Pass only server-resolved organization/connection
IDs. Add a separate domain outcome after authoritative processing; HTTP 200 must
not substitute for it. Use `activity_context` around a logical operation so
children and published tasks inherit correlation. Only set a deadline when a real
next step is expected and a later transition will clear it. Add the boundary and
its limitations to `coverage.py`, and verify privacy, failure and tenant isolation.
