# Performance and capacity testing

Performance evidence is stored as versioned JSON under `docs/performance/results/`. Never run a named large profile against a developer or production database. Use an isolated, production-shaped staging environment and retain database/Redis/container/provider dashboards with the result.

## Local service probe

`scripts/performance/run_capacity_profile.py` materializes exactly the declared profile and measures CRM dashboard/search/detail p50, p95, p99, RPS, errors, query counts, Redis latency, process RSS, and database connections. `smoke` is safe by default. Profiles A–D require `--allow-large` because they can create millions of rows.

```bash
GIT_SHA=$(git rev-parse HEAD) venv/bin/python scripts/performance/run_capacity_profile.py \
  --profile smoke --run-id release-name \
  --output docs/performance/results/after-release-name-smoke.json
```

Profiles in `capacity_profiles.json` are workloads to execute, not capacity claims:

- A: 100 organizations, 5 users/org, 1,000 leads/org, concurrency 25.
- B: 500 organizations, 10 users/org, 5,000 leads/org, concurrency 75.
- C: 1,000 organizations, 20 users/org, 10,000 leads/org, concurrency 150.
- D: 10 organizations, 200 users/org, 200,000 leads/org, concurrency 250.

The profile runner materializes the declared organizations, users, and leads. Add message/AI fixtures only with recorded size and provider stubs or a dedicated quota. A result is invalid if fixture creation was incomplete.

## Staging HTTP matrix

Copy `http_scenarios.example.json` outside source control, fill disposable fixture IDs and exact payloads, and enable scenarios deliberately. Never put cookies, CSRF values, OAuth tokens, provider secrets, or customer data in the manifest.

```bash
export SHVYA_LOAD_COOKIE='shvya_crm_sessionid=...'
export SHVYA_LOAD_CSRF_TOKEN='...'
export SHVYA_LOAD_VAR_LEAD_ID='...'
venv/bin/python scripts/performance/run_http_load.py \
  --base-url https://staging.example.invalid \
  --manifest /secure/path/scenarios.json \
  --iterations 200 --concurrency 20 \
  --output docs/performance/results/staging-release.json
```

The manifest names all 18 required flows: CRM dashboard/search/pipeline/detail/save/stage, WhatsApp inbox/inbound/outbound, AI enqueue/completion, Hosted AI, workflows, cadence, calendar, sales documents, support, and Operations MCP. Read-only scenarios are enabled in the example; mutating/provider scenarios remain disabled until disposable fixtures, idempotency keys, signed webhooks, and provider stubs/quotas are configured.

During every run capture the protected runtime snapshot and infrastructure graphs: database p95/CPU/storage/connections/locks, Redis memory/latency/evictions, per-queue depth/age/execution, AI provider latency/errors, active WebSockets and reconnects, gateway session/RSS/CPU/reconnect/history state, and container CPU/RSS. Record provider throttles separately from application errors.

## Baseline and interpretation

The untouched `77ba40ae` smoke baseline used one isolated organization, 100 leads, concurrency 4, and 40 operations per scenario. It recorded zero errors; dashboard 84.722/205.103/214.534 ms p50/p95/p99 at 36.217 RPS; search 19.610/25.352/29.459 ms at 193.768 RPS; detail 15.502/20.031/21.383 ms at 242.638 RPS. Query counts were 11/1/1 and Redis SET p50/p95/p99 was 0.395/0.471/1.275 ms.

This is a laptop service-level smoke result, not a production capacity envelope. It did not measure real HTTP middleware, provider calls, message/AI throughput, WebSocket stability, or sustained resource saturation. Compare only identical fixtures, code paths, concurrency, hardware, database state, and warm-up conditions. A release passes when errors remain zero and agreed endpoint/queue/provider SLOs hold without exhausting downstream headroom; no organization-count claim follows from a smoke run.

Use `scripts/performance/compare_results.py before.json after.json --output comparison.json` to store percentage deltas. The tool intentionally does not invent a release threshold; the service owner must define SLOs before a production-shaped run.
