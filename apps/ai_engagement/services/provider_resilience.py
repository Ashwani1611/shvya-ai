from __future__ import annotations

import hashlib
import os

from django.core.cache import cache


_PREFIX = "shvya:ai:provider-breaker:v1"


def _positive_int(name: str, default: int, *, minimum: int = 1, maximum: int = 3600) -> int:
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _key(kind: str, model: str) -> str:
    digest = hashlib.sha256(str(model or "unknown").encode("utf-8")).hexdigest()[:24]
    return f"{_PREFIX}:{kind}:{digest}"


def failure_threshold() -> int:
    return _positive_int("OPENAI_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 4, maximum=50)


def failure_window_seconds() -> int:
    return _positive_int("OPENAI_CIRCUIT_BREAKER_WINDOW_SECONDS", 60, maximum=3600)


def cooldown_seconds() -> int:
    return _positive_int("OPENAI_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 45, maximum=900)


def is_open(model: str) -> bool:
    try:
        return bool(cache.get(_key("open", model)))
    except Exception:
        # Cache health must never become an AI availability dependency.
        return False


def record_transient_failure(model: str) -> bool:
    """Record a retryable provider failure and open the breaker when needed.

    Returns True when the breaker is open after this failure.
    """

    count_key = _key("failures", model)
    try:
        if cache.add(count_key, 1, timeout=failure_window_seconds()):
            count = 1
        else:
            try:
                count = int(cache.incr(count_key))
            except (TypeError, ValueError):
                cache.set(count_key, 1, timeout=failure_window_seconds())
                count = 1

        if count >= failure_threshold():
            cache.set(_key("open", model), "1", timeout=cooldown_seconds())
            return True
        return False
    except Exception:
        return False


def record_success(model: str) -> None:
    try:
        cache.delete_many((_key("failures", model), _key("open", model)))
    except Exception:
        return
