# Production scaling and operations

This is the operating contract for scaling SHVYA as a modular Django monolith. Capacity is expressed in traffic, data, connections, queue delay, active sockets, provider limits, and Hosted browser sessions—not in organization count alone.

## Runtime topology

Before this change, one public Nginx routed to one fixed three-process Gunicorn container and one Daphne container. General, realtime-AI, and Hosted-AI workers shared PostgreSQL directly; two worker concurrencies were hard-coded. Cache, Channels, Celery broker, and results used separate logical Redis databases in production, while the Hosted gateway shared cache DB 0. A single browser gateway owned all Hosted sessions.

The supported topology is now:

```text
external load balancer / Nginx
  ├── Gunicorn web x N ─┐
  ├── Daphne ASGI x N ──┼── PgBouncer (transaction mode) ── PostgreSQL
  └── Celery lanes x N ─┘
         ├── celery (general)
         ├── ai_realtime
         ├── hosted_ai
         ├── campaigns
         ├── ingestion
         └── automation

Redis logical DBs: cache /0, Channels /1, broker /2, results /3,
Hosted gateway leases /4

Hosted routing (durable account shard + fenced lease)
  ├── gateway shard A (bounded browser sessions)
  ├── gateway shard B
  └── gateway shard C

Private object storage: ordinary Django FileField media
Persistent encrypted local volume: support attachments only
```

No request, login, OAuth, rate-limit, lock, upload, or WebSocket group state requires Gunicorn stickiness. Django sessions and authoritative state remain in PostgreSQL; shared transient state is in Redis; scalable FileField data uses the configured private S3 backend. Multiple web and ASGI containers are therefore safe when they share the same PostgreSQL, Redis URLs, signing keys, credential-encryption keys, and object store.

## Observability

Set a long random `OBSERVABILITY_TOKEN`. A monitor can request `GET /health/runtime-metrics/` with `X-SHVYA-Observability-Token`; without the token the route returns 404. The snapshot contains:

- bounded cross-replica request count, endpoint latency buckets, errors, and slow/5xx events;
- PostgreSQL active connections, idle transactions, lock waiters, and longest transaction;
- per-queue broker depth and oldest queued-task age, plus Celery queue/execution latency, result, retry, and failure series;
- Redis ping latency, memory, clients, evictions, and rejected connections;
- active WhatsApp and Hosted WebSockets with crash-safe expiry;
- process max RSS, CPU time, and host load average;
- AI provider latency/errors/calls and credit reservation, release, and settlement failures;
- WhatsApp/Instagram inbound and outbound message creation rate;
- gateway `/health` shard, owner, sessions/max sessions, memory, CPU, reconnects, lease conflicts, callback/history failures.

Structured operational events carry correlation/task IDs and bounded organization, user, lead, account, and provider identifiers where available. They never include message bodies, prompts, access tokens, credentials, or raw provider payloads. Metric series are capped at 500 and expire after eight days so accidental label growth is bounded.

Suggested initial alerts are operational starting points, not capacity claims:

- 5xx rate above 1% for five minutes or p95 above the product SLO;
- any idle transaction above 60 seconds, lock waiters sustained for 60 seconds, or pool utilization above 80%;
- realtime queue oldest age above 5 seconds; general/campaign/ingestion oldest age above the relevant business SLO;
- Redis evictions or rejected connections above zero, memory above 70% warning / 85% critical;
- WebSocket disconnect spike, Hosted lease conflicts, or a gateway above 80% of its configured session cap;
- AI provider error spike, settlement failures above zero, or per-organization admission rejection growth.

Infrastructure-level PostgreSQL CPU/storage, container CPU/memory, disk I/O, and object-store errors must also be collected by the hosting platform. Application metrics cannot replace host and managed-service telemetry.

## PostgreSQL connection pooling

Production and staging Compose put PgBouncer 1.25.2 in transaction mode between every Django/Celery process and PostgreSQL. Django uses `CONN_MAX_AGE=0` and disables server-side cursors while pooling. Do not use session-level PostgreSQL state, session advisory locks, temporary tables that span transactions, or a server-side cursor across transactions.

Calculate client demand before changing replica counts:

```text
potential clients = web replicas × Gunicorn workers
                  + ASGI replicas × ASGI process count
                  + sum(Celery replicas × concurrency per lane)
                  + Beat and management-job allowance
```

`PGBOUNCER_MAX_CLIENT_CONN` must exceed that demand plus deployment overlap. `PGBOUNCER_MAX_DB_CONNECTIONS` and `DEFAULT_POOL_SIZE` must fit below PostgreSQL `max_connections` after reserving monitoring, migration, superuser, and maintenance connections. Start with the committed defaults only for staging, measure pool wait and database CPU, and tune. More pool connections can reduce queueing but can also make PostgreSQL slower.

Deployment validation:

1. Back up PostgreSQL and confirm restore credentials before the first pooler rollout.
2. Start PgBouncer and verify its health check.
3. Run migrations as a one-off process through the same pooler unless a migration explicitly requires a direct/session connection.
4. Deploy one web/worker replica, exercise transactions, streaming/iterator jobs, and queue tasks.
5. Scale one lane at a time and monitor pool wait, active DB connections, transaction age, locks, DB CPU, and p95.
6. Roll back by setting `DB_HOST` to PostgreSQL, `DB_USE_PGBOUNCER=False`, and an explicitly bounded `DB_CONN_MAX_AGE`; do not leave half the fleet on incompatible settings.

## Web, ASGI, and Celery scale-out

Production no longer publishes the web container's port to the host; Nginx is the ingress. Scale web/ASGI replicas behind a load balancer that supports WebSocket upgrade and a drain period. Restart/reload Nginx after changing Compose replica membership unless the external load balancer provides dynamic discovery. Sticky sessions are not required.

WebSocket authentication binds users to their organization before joining tenant-specific groups. Shared Channels Redis carries broadcasts across ASGI replicas. Heartbeats refresh shared presence; stale entries expire. Clients must reconnect and refetch current durable chat state after disconnect. Delivery is at-least-once at the broadcast layer, so browser rendering must continue to key messages by durable message/provider ID rather than arrival count.

Celery concurrency is configured with environment variables. Realtime and Hosted AI keep priority isolation. Campaigns are bounded, durable, and account-round-robin; ingestion is isolated because document work is CPU/provider-heavy; automation is isolated because scans can burst. `prefetch_multiplier=1`, child recycling, and memory caps reduce head-of-line blocking and leaks.

Scale a queue only when its oldest age or depth grows while workers are saturated. First check downstream PostgreSQL/Redis/provider headroom. Scaling a provider-bound queue past its external quota increases retries rather than throughput. Keep task idempotency keys and durable claim rows; never treat Celery publication as the source of truth.

## Fairness and provider controls

- HTTP bursts have a shared, per-organization active-request admission limit. It returns 429 with a short retry hint and fails open during a Redis incident.
- AI starts have shared global and per-organization minute buckets. Deferred tasks republish with jitter without claiming execution or consuming their retry budget.
- Realtime AI has a separate queue from campaigns, ingestion, automation, and Hosted AI.
- Campaign dispatch takes bounded batches per sender account and recipient tasks keep durable idempotency state. Account-level sending policy and Celery rate limits remain in force.
- Existing provider adapters own timeouts and bounded retry decisions; tasks use delayed retries/backoff rather than tight loops. Unknown-outcome sends are not blindly duplicated.

The three shared admission limits default to zero (disabled) because this change has no production/provider saturation evidence from which to derive a safe number. Enable and tune global, organization, account, and provider limits in staging from observed saturation, 429/Retry-After data, SLOs, and contracted quotas. Add jitter whenever a provider supplies a common reset time.

High-value cache candidates are immutable or explicitly invalidated organization feature configuration, pipeline/stage metadata, and read-only dashboard metadata. Mutable lead/message state remains authoritative in PostgreSQL. Every cache key must include an organization or globally immutable namespace; never cache an unscoped CRM queryset.

## Database review

The high-volume model review covered leads, WhatsApp/Instagram messages, campaign delivery/events, AI traces/actions, follow-up state, trigger/workflow history, support, calls, bookings, sales tracking, and operations MCP audit data. Existing schema already includes the key tenant/time, lead/time, account/time, due/status, provider-ID idempotency, trigram, and JSON GIN indexes used by those paths. The CRM smoke probe holds dashboard queries to 11 and lead search/detail to one each. Inbox paths use bounded or keyset/windowed reads rather than loading an unbounded thread.

One new composite index supports the actual Hosted control-plane lookup `(connection_type, hosted_gateway_shard, status)`. No speculative message indexes or table partitioning were added. Before adding another index, capture `EXPLAIN (ANALYZE, BUFFERS)` against production-shaped staging data and account for write amplification. Watch windowed Hosted conversation heads and unread aggregates first as message volume grows.

Critical paths retain explicit organization/account filtering; shard updates and callbacks additionally require account + organization + expected shard. Operations MCP cross-tenant tools retain explicit privileged authorization. The regression suite must include cross-tenant object IDs, account IDs, sockets, and MCP mutations on every release.

## Retention and partition readiness

| Data | Online retention starting policy | Archive/delete approach | Partition candidate and trigger |
|---|---|---|---|
| WhatsApp/Instagram messages | Product/legal decision; default no automatic deletion | Export tenant-scoped encrypted archive, then delete in bounded time/PK batches | Monthly `created_at` partitions only after plans show time-range scans and maintenance/vacuum cannot meet SLO at production volume |
| AI traces/action receipts | 90 days detailed; preserve billing/audit summary as required | Redact content first, archive aggregate metadata, bounded purge | Monthly partitions when trace table/index size or purge vacuum materially affects DB p95 |
| Workflow/trigger execution history | 90 days detailed, 12 months summary | Object-store export and bounded purge | Monthly by start/created time when due scans lose index efficiency or retention jobs exceed their window |
| Activity/audit/MCP events | At least 12 months or compliance policy | Immutable encrypted archive with integrity metadata | Monthly partitions when audit indexes exceed working memory/cache or deletes cause bloat |
| Campaign delivery/events | 90 days recipient/event detail, longer campaign summary | Existing event cleanup plus tenant export | Monthly partitions when campaign event cleanup/vacuum misses its maintenance window |
| Call/booking/sales tracking | Product/compliance policy | Archive closed history, retain transactional records | Partition only after measured time-range workloads and restore requirements justify it |

Retention must be configurable per contractual/compliance requirement. A purge must be resumable, tenant-scoped, rate-limited, observable, and tested with backup restore. Never use a single huge delete transaction.

## Redis separation thresholds

Logical DB separation prevents cache maintenance from deleting Channels/Celery/gateway keys, but it does not isolate CPU, memory, network, persistence, or failure. Move workloads to separate Redis services when any of these are observed: sustained memory above 70%, eviction/rejection, broker latency harming cache p95, Channels fanout causing broker delay, persistence policy conflicts, different availability requirements, or one workload needing independent maintenance. Broker and gateway lease data should be the first candidates for durable, independently operated Redis; cache may remain disposable. Configure `maxmemory`/policy per service only after physical separation because a single policy affects all logical DBs.

## Object storage, replicas, backups, and disaster recovery

With `USE_S3_STORAGE=True`, ordinary FileFields—logos, WhatsApp/follow-up media, sales PDFs and attachments, calendar submissions, and AI knowledge files—use private S3 storage, authenticated signed URLs, server-side encryption, and unique object names. Bucket public access must remain blocked. Support attachments deliberately retain application-level encrypted local storage on a persistent volume; include that volume and its encryption keys in backups until an equivalent encrypted object backend is implemented.

Do not route transactional CRM reads to a replica. A future router may send explicitly marked analytics, historical exports, and delay-tolerant dashboards to a replica only after replica-lag metrics and read-after-write expectations are defined. Fall back to primary when lag exceeds the workload's bound; writes, locks, authentication, inbox state, tasks, and MCP mutations always use primary.

Production requirements:

- encrypted PostgreSQL full backups plus WAL/PITR, cross-failure-domain copies, quarterly restore drills, and documented RPO/RTO;
- bucket versioning/lifecycle and cross-region or provider durability appropriate to the business RPO;
- Redis treated as rebuildable for cache/results but protected appropriately for broker and active Hosted leases; durable PostgreSQL task/session state is the recovery source;
- Hosted auth/session volumes mapped one shard at a time, encrypted, backed up consistently, and never mounted read-write by two gateways;
- offline escrow and tested rotation/recovery for `SECRET_KEY`, JWT signing, object-store credentials, Hosted callback/gateway tokens, and current/fallback credential-encryption keys;
- rollback uses the previous immutable image and compatible migration state. Use additive migrations first; never roll application code back across an irreversible schema change without a tested forward repair.

## Future extraction seams (not services today)

| Candidate | Measurable trigger | Contract and data ownership | Rollback complexity |
|---|---|---|---|
| Hosted session platform | Gateway fleet/session recovery cannot meet SLO, independent release cadence is required, or Chromium resource isolation dominates the app hosts | Django owns account/tenant policy; gateway owns browser runtime. Authenticated idempotent commands/events keyed by account, shard, owner, and provider message ID | High: session directories and lease fencing require controlled drain/reattach |
| AI execution platform | AI queue/provider traffic independently saturates worker hosts or needs a distinct compliance/accelerator boundary | Django owns CRM, credits, prompt policy, and durable job/result records; executor accepts idempotent job IDs and returns usage/result events | Medium-high: dual execution must be prevented and credit settlement reconciled |
| Messaging ingestion/delivery | Sustained webhook/send rate causes independent database or deploy SLO pressure despite queue and database tuning | Django retains organization/account/CRM state; edge validates provider signatures and publishes deduplicated envelopes keyed by provider ID | High: ordering, retry ownership, and unknown-outcome sends require replay tooling |

Organization, users, leads, pipelines, stages, CRM attributes, and core CRM transactions remain centralized. Extraction needs measured saturation plus an operational owner; repository size is not a trigger.
