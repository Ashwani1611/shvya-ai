# Hosted WhatsApp gateway: owned browser lifecycle

## Scope

This fix addresses Chromium orphan reaping and unsafe session replacement in the
Hosted WhatsApp gateway. It does not establish why production Chromium originally
exited; correlate the new exit diagnostics with container resource and OOM data.
No database migrations, authentication-volume changes, or staging startup are needed.

## Runtime invariants

- Production Compose enables `init: true` and `stop_grace_period: 60s`. Init owns
  PID 1, forwards signals, and reaps adopted children. Standalone Docker runs must
  use `--init`; do not run this browser gateway with bare Node as PID 1.
- Create, refresh, logout, restore, expiry, fencing and shutdown are serialized
  per session. In-flight reservations also enforce the gateway-wide capacity.
- Cleanup is idempotent. It observes initialization, including a browser assigned
  after teardown starts. Lease release and replacement happen only after verified
  process exit; sending a signal is not proof of exit.
- Teardown first attempts `client.destroy()`, then TERM at eight seconds and KILL
  three seconds later, allowing another three seconds for exit. Escalation targets
  only the recorded browser and identity-checked members of its dedicated process
  group. No global process-name kill is used.
- A renderer surviving its browser root is still tracked. Linux process identities
  are checked before signals; ambiguous ownership fails closed.
- Unsafe cleanup or an uncancelled logout/profile-deletion operation stops new
  lifecycle work and exits the gateway with failure. Docker can recycle that
  container; unconfirmed leases are left to expire rather than handed to a second
  browser. This fail-safe may interrupt other Hosted sessions on the same gateway.
- Normal shutdown never logs out or deletes authenticated LocalAuth profiles.
  Explicit logout and abandoned-QR expiry delete profiles only after browser exit.
  Stale restore scans cannot resurrect a session that was explicitly logged out.
- Old-generation event handlers and delayed callback retries are fenced. Shutdown
  returns HTTP 503 for health and session requests. Puppeteer's per-browser signal
  handlers are disabled so the gateway's single shutdown owner remains in control.

## Recovery and diagnostics

Initialization/browser failures use exponential backoff starting at 30 seconds,
with up to 20% jitter. Automatic recovery pauses on the sixth consecutive failure
in the running gateway process. A stable running session resets the failure streak
when a later failure occurs; an explicit Refresh QR resets the circuit breaker.
Retry state is in memory and resets when the gateway process is replaced.

`/health` retains existing metrics and adds lifecycle diagnostics, browser exit,
TERM/KILL, cleanup-failure and retry-pause counters. Session responses include
`retryPaused` and `retryAt`. Exit logs identify the session, owned PID, exit code,
signal and whether shutdown was expected; they do not log QR codes or credentials.
The existing Node memory metrics are not total Chromium/container memory usage.

## Build and regression tests

The final `patch-hosted-process-lifecycle.js` transformation is applied after all
existing source patches. It retains their build-time signatures, replaces lifecycle
entry points with the shared controller, and validates the generated JavaScript.
The existing mandatory CI gateway test entry point includes the new tests:

```bash
node --test whatsapp_web_gateway/tests/hosted-sync.test.js
```

The isolated controller tests can also be run without installing dependencies:

```bash
node --test whatsapp_web_gateway/tests/session-lifecycle.cases.js
```

Coverage includes concurrent creation, capacity reservations, late initialization,
failed cleanup, stale callbacks, lease ordering, backoff, logout/QR expiry, shutdown,
a real TERM-resistant child, and a renderer surviving its process-group root.
The production-chain test also boots the generated gateway with mocked channels.
These tests do not substitute for a real authenticated WhatsApp smoke test.

## Production rollout and verification

Use the existing production Compose project/environment flags. Keep staging
stopped. The changed image must be rebuilt and the gateway recreated; a restart
alone does not apply `init: true` or the new code. Recreating the gateway briefly
interrupts Hosted sessions but retains the existing `whatsapp_sessions` volume.
Never use `down -v` for this repair.

```bash
cd /opt/shvya-ai || exit 1
docker compose --env-file .env -f docker-compose.yml config -q
docker compose --env-file .env -f docker-compose.yml \
  up -d --no-deps --build --force-recreate whatsapp-web-gateway

cid="$(docker compose --env-file .env -f docker-compose.yml ps -q whatsapp-web-gateway)"
[ -n "$cid" ] || { echo 'Gateway container not found'; exit 1; }
docker inspect --format 'Init={{.HostConfig.Init}} Health={{.State.Health.Status}}' "$cid"
docker exec "$cid" cat /proc/1/comm
docker top "$cid" -eo pid,ppid,stat,comm |
  awk 'NR > 1 && $3 ~ /^Z/ {n++} END {print "Zombie processes:", n+0}'
```

Confirm init is enabled, PID 1 is not Node, health becomes healthy, and zombie
counts do not accumulate during controlled lifecycle tests. Confirm one existing
account reconnects without a fresh QR and perform one authorized incoming/outgoing
message smoke test. Record the production commit and inspect resource/OOM events
before claiming the original crash cause is resolved.
