"""Low-overhead, shared runtime metrics and structured operational events.

Metrics use the configured shared Django cache so multiple web/ASGI workers
contribute to the same bounded registry. Detailed provider/customer content is
never recorded. Metric labels stay low-cardinality; customer identifiers belong
only in structured operational events.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone as datetime_timezone
import hashlib
import json
import logging
import math
import time
import uuid

from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger("shvya.operations")

REQUEST_ID = ContextVar("shvya_request_id", default="")
TASK_ID = ContextVar("shvya_task_id", default="")
METRIC_PREFIX = "shvya:metrics:v1"
REGISTRY_KEY = f"{METRIC_PREFIX}:registry"
REGISTRY_LOCK_KEY = f"{METRIC_PREFIX}:registry-lock"
METRIC_TTL_SECONDS = 8 * 24 * 60 * 60
MAX_REGISTERED_SERIES = 500
LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 15000, 60000)


def _clean(value, *, maximum=120):
    text = str(value or "").strip()
    return text[:maximum]


def correlation_id(value=""):
    candidate = _clean(value, maximum=64)
    try:
        return str(uuid.UUID(candidate))
    except (TypeError, ValueError):
        return str(uuid.uuid4())


def bind_context(*, request_id="", task_id=""):
    if request_id:
        REQUEST_ID.set(_clean(request_id, maximum=64))
    if task_id:
        TASK_ID.set(_clean(task_id, maximum=64))


def clear_context():
    REQUEST_ID.set("")
    TASK_ID.set("")


def emit_event(event, *, level=logging.INFO, **fields):
    payload = {
        "timestamp": datetime.now(datetime_timezone.utc).isoformat(),
        "event": _clean(event, maximum=100),
    }
    request_id = REQUEST_ID.get()
    task_id = TASK_ID.get()
    if request_id:
        payload["request_id"] = request_id
    if task_id:
        payload["task_id"] = task_id
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, (bool, int, float)):
            payload[key] = value
        else:
            payload[key] = _clean(value, maximum=240)
    logger.log(level, json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _normalized_labels(labels):
    return tuple(
        sorted(
            (str(key)[:40], _clean(value, maximum=80))
            for key, value in (labels or {}).items()
            if value is not None and value != ""
        )
    )


def _series(metric, labels):
    normalized = _normalized_labels(labels)
    encoded = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
    name = "".join(ch for ch in str(metric) if ch.isalnum() or ch in "_.-")[:80]
    return name, normalized, f"{name}:{digest}"


def _register(metric, labels, series):
    """Register a bounded metric series before any per-series Redis keys exist."""
    try:
        registry = list(cache.get(REGISTRY_KEY, []) or [])
        if any(item.get("series") == series for item in registry):
            return True
        if len(registry) >= MAX_REGISTERED_SERIES:
            return False

        # Serialize only the rare new-series path. Existing-series updates stay
        # lock-free. If another replica owns the registration lock, drop this
        # first sample rather than create an unregistered high-cardinality key.
        if not cache.add(REGISTRY_LOCK_KEY, "1", timeout=2):
            registry = list(cache.get(REGISTRY_KEY, []) or [])
            return any(item.get("series") == series for item in registry)

        try:
            registry = list(cache.get(REGISTRY_KEY, []) or [])
            if any(item.get("series") == series for item in registry):
                return True
            if len(registry) >= MAX_REGISTERED_SERIES:
                return False
            registry.append(
                {
                    "metric": metric,
                    "labels": dict(labels),
                    "series": series,
                }
            )
            cache.set(REGISTRY_KEY, registry, timeout=METRIC_TTL_SECONDS)
            return True
        finally:
            cache.delete(REGISTRY_LOCK_KEY)
    except Exception:
        logger.debug("Metric registry update failed", exc_info=True)
        return False


def _increment(key, amount=1):
    try:
        if cache.add(key, int(amount), timeout=METRIC_TTL_SECONDS):
            return
        cache.incr(key, int(amount))
    except (ValueError, TypeError):
        cache.set(key, int(amount), timeout=METRIC_TTL_SECONDS)
    except Exception:
        logger.debug("Metric increment failed", exc_info=True)


def increment(metric, *, labels=None, amount=1):
    name, normalized, series = _series(metric, labels)
    if not _register(name, normalized, series):
        return
    _increment(f"{METRIC_PREFIX}:counter:{series}", amount)


def observe_latency(metric, milliseconds, *, labels=None):
    try:
        value = max(0.0, float(milliseconds))
    except (TypeError, ValueError):
        return
    if not math.isfinite(value):
        return
    name, normalized, series = _series(metric, labels)
    if not _register(name, normalized, series):
        return
    _increment(f"{METRIC_PREFIX}:hist:{series}:count", 1)
    _increment(f"{METRIC_PREFIX}:hist:{series}:sum_us", round(value * 1000))
    for boundary in LATENCY_BUCKETS_MS:
        if value <= boundary:
            _increment(f"{METRIC_PREFIX}:hist:{series}:le:{boundary}", 1)


def _percentile_from_buckets(count, buckets, percentile):
    if not count:
        return 0
    target = math.ceil(count * percentile)
    for boundary in LATENCY_BUCKETS_MS:
        if int(buckets.get(str(boundary), 0) or 0) >= target:
            return boundary
    return f">{LATENCY_BUCKETS_MS[-1]}"


def metrics_snapshot():
    try:
        registry = list(cache.get(REGISTRY_KEY, []) or [])
    except Exception:
        return {"available": False, "series": []}

    series_data = []
    for item in registry[:MAX_REGISTERED_SERIES]:
        try:
            series = item.get("series", "")
            counter = cache.get(f"{METRIC_PREFIX}:counter:{series}")
            count = cache.get(f"{METRIC_PREFIX}:hist:{series}:count")
            if counter is not None:
                value = {"count": int(counter or 0)}
            elif count is not None:
                count = int(count or 0)
                buckets = {
                    str(boundary): int(
                        cache.get(f"{METRIC_PREFIX}:hist:{series}:le:{boundary}", 0)
                        or 0
                    )
                    for boundary in LATENCY_BUCKETS_MS
                }
                sum_us = int(
                    cache.get(f"{METRIC_PREFIX}:hist:{series}:sum_us", 0) or 0
                )
                value = {
                    "count": count,
                    "mean_ms": round((sum_us / 1000) / max(1, count), 3),
                    "p50_ms_upper_bound": _percentile_from_buckets(count, buckets, 0.50),
                    "p95_ms_upper_bound": _percentile_from_buckets(count, buckets, 0.95),
                    "p99_ms_upper_bound": _percentile_from_buckets(count, buckets, 0.99),
                    "buckets": buckets,
                }
            else:
                continue
        except Exception:
            return {"available": False, "series": series_data}
        series_data.append(
            {
                "metric": item.get("metric"),
                "labels": dict(item.get("labels") or {}),
                **value,
            }
        )
    return {"available": True, "series": series_data}


def request_dimensions(request, response=None):
    match = getattr(request, "resolver_match", None)
    endpoint = getattr(match, "view_name", "") or "unresolved"
    user = getattr(request, "crm_user", None) or getattr(request, "user", None)
    return {
        "method": request.method,
        "endpoint": endpoint,
        "status_group": f"{int(getattr(response, 'status_code', 0) or 0) // 100}xx",
        "organization_id": getattr(user, "organization_id", ""),
        "user_id": getattr(user, "id", ""),
    }


def request_metric_labels(dimensions, *, status_group=None):
    """Keep customer identifiers out of metric dimensions."""
    return {
        "method": dimensions["method"],
        "endpoint": dimensions["endpoint"],
        "status_group": status_group or dimensions["status_group"],
    }


class RequestObservabilityMiddleware:
    """Attach correlation IDs and aggregate endpoint latency/error metrics."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = correlation_id(request.headers.get("X-Request-ID", ""))
        request.shvya_request_id = request_id
        bind_context(request_id=request_id)
        started = time.perf_counter()
        response = None
        try:
            response = self.get_response(request)
            return response
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            dimensions = request_dimensions(request)
            increment(
                "http.requests",
                labels=request_metric_labels(dimensions, status_group="5xx"),
            )
            increment("http.errors", labels={"endpoint": dimensions["endpoint"]})
            observe_latency(
                "http.request_duration_ms",
                elapsed_ms,
                labels={"endpoint": dimensions["endpoint"], "method": request.method},
            )
            emit_event(
                "http.request.failed",
                level=logging.ERROR,
                duration_ms=round(elapsed_ms, 3),
                path=dimensions["endpoint"],
                method=request.method,
                organization_id=dimensions["organization_id"],
                user_id=dimensions["user_id"],
            )
            raise
        finally:
            if response is not None:
                elapsed_ms = (time.perf_counter() - started) * 1000
                dimensions = request_dimensions(request, response)
                increment("http.requests", labels=request_metric_labels(dimensions))
                if response.status_code >= 500:
                    increment("http.errors", labels={"endpoint": dimensions["endpoint"]})
                observe_latency(
                    "http.request_duration_ms",
                    elapsed_ms,
                    labels={"endpoint": dimensions["endpoint"], "method": request.method},
                )
                slow_ms = float(getattr(settings, "OBSERVABILITY_SLOW_REQUEST_MS", 1000))
                if response.status_code >= 500 or elapsed_ms >= slow_ms:
                    emit_event(
                        "http.request.completed",
                        level=(logging.ERROR if response.status_code >= 500 else logging.WARNING),
                        duration_ms=round(elapsed_ms, 3),
                        endpoint=dimensions["endpoint"],
                        method=request.method,
                        status=response.status_code,
                        organization_id=dimensions["organization_id"],
                        user_id=dimensions["user_id"],
                    )
                response["X-Request-ID"] = request_id
            clear_context()
