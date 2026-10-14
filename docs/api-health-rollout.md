# API health and automatic review discovery

## Scope and count consistency

The Superadmin-only read endpoint `/superadmin/api/health/data/` accepts the
same `range`, `organization`, `integration`, `kind`, and `q` filters as the
activity explorer. The backward-compatible `hours` parameter accepts
1, 24, 168, 720 and 2160 (90 days). Supplying inconsistent `hours` and `range`
returns HTTP 400 rather than silently selecting a different time window.

Use `organization=<UUID>` for a tenant or `organization=unattributed` for
records with no resolved organisation. Invalid values fail closed. The same
unattributed selection is supported by the explorer and investigation inbox.
The organisation selector includes this explicit option.

The health table, overview cards and attention drill-downs share filtering and
attention definitions. Health compares all outcomes within the selected scope;
like the overview cards, it intentionally ignores the outcome tab. Links retain
time, tenant, integration, type and search, while resetting page/outcome as
needed. A 90-day selection now uses 2160 hours, not 720; one-hour health links
open the exact one-hour explorer window rather than expanding to 24 hours.

`attention` includes failed, unknown, blocked, partial and overdue open records.
The compatibility field `failures` retains its former issue-status-only meaning;
`overdue` is returned separately. The UI displays `attention` and labels its
link **Inspect attention**, because overdue is not proof of failure. Unknown
is a subset of attention, not an additional count to add to it. The top 12
organisations are shown in HTML; the JSON report returns up to 100, ordered by
attention count and then recorded volume. These are instrumented steps, not
business-transaction counts, message-delivery confirmation or an uptime SLA.

## Discovery and bounded backlog recovery

Celery Beat schedules `core.discover_api_incidents` every five minutes on the
existing general worker. Capture-disabled installations skip scheduled discovery.
The task scans at most 200 retained attention activities with no existing review,
oldest first, and no creation-time cutoff. Older stuck work and old unreviewed
failures therefore remain eligible while their evidence is retained. This does
not recover records already removed by retention or evidence never captured.

The manual command uses the same no-age-cutoff default with a 100-record limit:

```bash
python manage.py discover_api_incidents --limit 100
```

An optional bounded manual lookback uses the activity's **updated** time for
issue outcomes, rather than its creation time. Overdue open work is included
regardless of either timestamp:

```bash
python manage.py discover_api_incidents --hours 24 --limit 100
```

Limits remain 1-500 records per invocation; explicit lookbacks are 1-720 hours.
Oldest-first batches drain remaining retained backlog across subsequent runs.
Activity row locks, a final attention recheck and the unique activity/review link
prevent duplicate review creation. Existing resolved reviews are not reopened.
Review creation never changes the underlying message or API outcome.

No provider calls, message resends, webhook replay or external notification
sending are introduced. Existing authentication, CSRF protections, redaction,
retention policy and capture switches remain in effect. A larger historical
review backlog may become visible after rollout; monitor worker execution and
unresolved-review storage. A successful deployment alone does not prove Beat
execution or end-to-end provider delivery.

## Validation and rollout

Regression coverage lives in `apps/superadmin/tests/test_api_health.py`, alongside
existing API explorer, investigation, privacy and access-control tests. It covers
90-day and one-hour windows; all scope filters and clicked links; unattributed
isolation; overdue attention counts; old failures; late outcomes; explicit/manual
lookbacks; scheduled capture gating; bounded batch progress; duplicate prevention;
resolved-review preservation; invalid filters and read-only access.

No schema migration is required. Deploy the validated main commit using the
existing deployment workflow, collect static assets and restart web, workers
and Beat. Verify the selected time window, organisation counts and clicked
attention results in the authenticated Superadmin page. Provider reconciliation,
authoritative transaction-impact totals and safe retry adapters remain separate
work and are not enabled by these fixes.
