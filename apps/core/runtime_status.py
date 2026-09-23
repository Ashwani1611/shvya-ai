"""Protected operational snapshot for platform monitoring and runbooks."""

from __future__ import annotations

import json
import os
import resource
import secrets
import sys
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET
import redis

from apps.core.observability import metrics_snapshot


def _authorized(request):
    expected = str(getattr(settings, "OBSERVABILITY_TOKEN", "") or "")
    supplied = str(request.headers.get("X-SHVYA-Observability-Token", "") or "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _database_snapshot():
    result = {
        "available": False,
        "active_connections": None,
        "idle_in_transaction": None,
        "lock_waiters": None,
        "longest_transaction_seconds": None,
    }
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (WHERE state = 'active'),
                    count(*) FILTER (WHERE state = 'idle in transaction'),
                    count(*) FILTER (WHERE wait_event_type = 'Lock'),
                    COALESCE(EXTRACT(EPOCH FROM (
                        clock_timestamp() - min(xact_start)
                    )), 0)
                FROM pg_stat_activity
                WHERE datname = current_database()
                """
            )
            active, idle_tx, lock_waiters, longest = cursor.fetchone()
        result.update(
            {
                "available": True,
                "active_connections": active,
                "idle_in_transaction": idle_tx,
                "lock_waiters": lock_waiters,
                "longest_transaction_seconds": round(float(longest or 0), 3),
            }
        )
    except Exception as exc:  # pragma: no cover - depends on DB monitoring grants
        result["error"] = type(exc).__name__
    return result


def _redis_snapshot():
    result = {"available": False}
    try:
        client = redis.Redis.from_url(settings.CACHE_REDIS_URL)
        started = time.perf_counter()
        client.ping()
        latency_ms = (time.perf_counter() - started) * 1000
        info = client.info()
        result.update(
            {
                "available": True,
                "latency_ms": round(latency_ms, 3),
                "used_memory_bytes": int(info.get("used_memory", 0) or 0),
                "connected_clients": int(info.get("connected_clients", 0) or 0),
                "evicted_keys": int(info.get("evicted_keys", 0) or 0),
                "rejected_connections": int(info.get("rejected_connections", 0) or 0),
            }
        )
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def _celery_snapshot():
    queues = tuple(getattr(settings, "SHVYA_CELERY_QUEUES", ("celery",)))
    result = {"available": False, "queues": {}}
    try:
        broker = redis.Redis.from_url(settings.CELERY_BROKER_URL)
        broker.ping()
        now = time.time()
        for queue in queues:
            oldest = broker.zrange(f"shvya:queue-age:{queue}", 0, 0, withscores=True)
            result["queues"][queue] = {
                "depth": int(broker.llen(queue)),
                "oldest_queued_seconds": (
                    round(max(0, now - float(oldest[0][1])), 3) if oldest else 0
                ),
            }
        result["available"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def _websocket_snapshot():
    result = {"available": False, "active": {}}
    try:
        layer_url = str(settings.CHANNEL_LAYER_REDIS_URL)
        parsed = urlsplit(layer_url)
        if parsed.scheme not in {"redis", "rediss"}:
            return result
        client = redis.Redis.from_url(layer_url)
        cutoff = time.time() - int(getattr(settings, "WEBSOCKET_METRIC_STALE_SECONDS", 90))
        for kind in ("whatsapp", "hosted_whatsapp"):
            key = f"shvya:websockets:{kind}"
            client.zremrangebyscore(key, 0, cutoff)
            result["active"][kind] = int(client.zcard(key))
        result["available"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def _process_snapshot():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    max_rss = int(usage.ru_maxrss)
    max_rss_bytes = max_rss if sys.platform == "darwin" else max_rss * 1024
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        load_1m = load_5m = load_15m = None
    return {
        "pid": os.getpid(),
        "max_rss_bytes": max_rss_bytes,
        "user_cpu_seconds": round(float(usage.ru_utime), 3),
        "system_cpu_seconds": round(float(usage.ru_stime), 3),
        "load_average": [load_1m, load_5m, load_15m],
    }


@require_GET
def runtime_metrics(request):
    if not _authorized(request):
        raise Http404
    payload = {
        "status": "ok",
        "application": metrics_snapshot(),
        "process": _process_snapshot(),
        "database": _database_snapshot(),
        "redis": _redis_snapshot(),
        "celery": _celery_snapshot(),
        "websockets": _websocket_snapshot(),
    }
    return JsonResponse(json.loads(json.dumps(payload, default=str)))
