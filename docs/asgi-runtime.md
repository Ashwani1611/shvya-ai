# ASGI runtime

The `ws` Compose service runs `config.asgi:application` through Uvicorn 0.54.0 with websockets 16.1.1. Gunicorn continues to serve ordinary HTTP in the separate `web` service. Nginx still routes `/ws/` to the private `ws:8001` endpoint. Django Channels, CRM session authentication, origin validation, tenant checks and the Redis channel layer are unchanged.

## Security reason

On 2026-10-16, the dependency audit identified [GHSA-8pqf-f4m5-798g / CVE-2026-106454](https://github.com/twisted/twisted/security/advisories/GHSA-8pqf-f4m5-798g) in Twisted 26.4.0, installed by Daphne. A patched stable Twisted release was not available. Replacing the ASGI protocol runner removes that dependency from newly built images; it does not suppress the advisory, omit dependencies from audit, or change the CI/Security release gates.

[Django documents Uvicorn deployment](https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/uvicorn/), and [Channels supports other ASGI servers](https://channels.readthedocs.io/en/stable/deploying.html#alternative-web-servers). Uvicorn is an ASGI server here, not a replacement application framework. Plain Uvicorn plus the explicit WebSocket dependency avoids unrelated development/reload and alternate-event-loop extras.

## Explicit process settings

The production and staging Compose definitions use the same flags:

| Setting | Value | Reason |
| --- | --- | --- |
| Application and bind | `config.asgi:application`, `0.0.0.0:8001` | Preserve the existing ASGI router and private Nginx upstream. |
| Workers | `1` | Preserve one process per ASGI replica even when `WEB_CONCURRENCY` exists. Scale with `ASGI_REPLICAS`. |
| Loop / HTTP parser | `asyncio` / `h11` | Use deterministic installed implementations without optional native extras. |
| WebSocket protocol | `websockets-sansio` | Use the supported SansIO implementation explicitly. |
| Lifespan | `off` | The existing Channels router supports HTTP and WebSocket scopes, not lifespan events. |
| Proxy headers | `--no-proxy-headers` | Preserve Daphne's previous default; do not newly trust arbitrary forwarded client addresses. Nginx continues to supply Host/Origin/cookie headers for the existing authentication path. |
| Incoming message cap | `1048576` bytes | Preserve Daphne 4.2.3's 1 MiB message limit instead of Uvicorn's larger default. The message cap also bounds fragmented messages. |
| Ping interval / timeout | `20` / `30` seconds | Preserve Daphne's keepalive timings. These control protocol pings separately from the application's heartbeat messages. |
| Graceful shutdown | `5` seconds | Bound shutdown within Compose's normal 10-second stop window. Clients reconnect and refetch durable state. |

The SansIO backend uses transport read backpressure; the legacy-only `--ws-max-queue` setting is not supplied. `WebSocketLifecycleMiddleware`, outside the origin/authentication stack, preserves Daphne's server-enforced 5-second handshake deadline, 24-hour connection lifetime and 10-second application-cleanup limit. Expired connections close with code 1001, and reconnecting clients must pass CRM session and tenant authorization again. This prevents the runner change from extending access indefinitely after session revocation. The wrapper wakes pending application receives for disconnect cleanup, rejects late acceptance/broadcasts and cancels stalled applications after the cleanup deadline. A coroutine that ignores cancellation cannot be forcibly killed by Python; the wrapper returns at its deadline, logs that condition and keeps the late task's socket sends blocked. HTTP requests remain unchanged. TLS continues to terminate at Nginx; the internal ASGI listener does not need HTTP/2 or a certificate.

## Verification and monitoring

Install into a clean environment or rebuild the application image. Updating an old virtual environment alone can leave the removed Daphne/Twisted packages installed. Run `pip check`, the unchanged strict dependency audit, and the full CI gate for the resulting requirements. Do not restore Daphne solely to make a test helper import succeed: `channels.testing` in Channels 4.1 imports its Daphne live-server helper eagerly. Actual-server tests can exercise the deployed Uvicorn command directly.

Validate both Compose definitions and test a real ASGI socket with the committed flags. Cover HTTP health through ASGI, successful CRM-cookie WebSocket authentication, missing/invalid sessions, rejected origins, inactive organizations, tenant/account isolation, Redis event delivery, oversized messages, disconnect cleanup and bounded process shutdown. Exercise both the WhatsApp inbox/thread and Hosted account routes. Keep the ordinary Gunicorn HTTP health check as well.

After the normal validated-main deployment, check `docker compose ps ws`, `docker compose logs --tail=100 ws`, and HTTP readiness. Confirm a logged-in browser establishes `/ws/` connections through public Nginx and receives live events. Check `/health/runtime-metrics/` with the existing observability credential for active sockets and disconnect changes; do not put that credential in logs. Monitor reconnect rates, ASGI memory/CPU, channel-layer failures and message latency against the preceding release.

## Rollback

Roll back the full tested application image and its matching Compose command through the repository's release process; do not change only the executable in a running container. A previous image containing affected Twisted reintroduces the vulnerability, so it is not a clean security remediation. Prefer a forward fix or another audited, validated release. Keep web, ASGI and workers on schema-compatible releases, and preserve database, Redis and provider state. The runner change itself adds no migrations; the blog release still requires its additive migration.
