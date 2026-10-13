# API health and automatic review discovery

This incremental change adds a **Superadmin-only** read-only health endpoint
at `/superadmin/api/health/data/?hours=24` (allowed: 1, 24, 168, 720).
Optionally pass `organization=<UUID>`. The response contains per-organization
recorded outcome counts, issue cohorts grouped by organization/integration/error,
and 20 recent attention activities with rules-based guidance and clickable
existing activity URLs. Counts reflect **instrumented activity records**, not
end-to-end message delivery, an SLA, or a complete count of customer transactions.

To create review tickets for recorded failed/unknown/blocked/partial or overdue
activities, run:

```bash
python manage.py discover_api_incidents --hours 24 --limit 100
```

The command is bounded and repeat-safe through a unique activity/incident link.
It **does not** resend messages, invoke provider APIs, change message state,
create alert notifications, or infer a verified root cause. Schedule as an
explicit operational job only after validating load and queue capacity.

## Next integration work

A complete incident platform still needs automatic production scheduling,
organisation-specific dashboard cards, impact counts from authoritative models,
external notification policy, provider reconciliation adapters, and strictly
idempotent recovery contracts. These have **not** been enabled by this change.
