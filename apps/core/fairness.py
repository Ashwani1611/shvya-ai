"""Shared, tenant-aware admission controls for horizontally scaled workers."""

from __future__ import annotations

import hashlib
import time

from django.core.cache import cache

from apps.core.observability import increment


def _bucket_key(scope, subject, window_seconds):
    window = int(time.time()) // int(window_seconds)
    digest = hashlib.sha256(str(subject).encode("utf-8")).hexdigest()[:24]
    return f"shvya:fairness:{scope}:{digest}:{window}"


def admit(*, scope, subject, limit, window_seconds=60):
    """Return ``(allowed, retry_after)`` for one shared fixed-window bucket."""
    limit = int(limit or 0)
    window_seconds = max(1, int(window_seconds))
    if limit <= 0:
        return True, 0
    key = _bucket_key(scope, subject, window_seconds)
    try:
        if cache.add(key, 1, timeout=window_seconds + 2):
            count = 1
        else:
            count = cache.incr(key)
    except (ValueError, TypeError):
        try:
            cache.set(key, 1, timeout=window_seconds + 2)
            count = 1
        except Exception:
            return True, 0
    except Exception:
        # Provider-level and task-level controls remain in force. A metrics or
        # cache incident must not become a platform-wide traffic outage.
        return True, 0
    if int(count) <= limit:
        return True, 0
    retry_after = max(1, window_seconds - (int(time.time()) % window_seconds))
    increment("fairness.throttled", labels={"scope": scope})
    return False, retry_after


def admit_ai_start(*, organization_id, organization_limit, global_limit):
    # Check the tenant bucket first. A tenant that is already above its own
    # allowance must not consume scarce global admission capacity and starve
    # unrelated organizations.
    allowed, delay = admit(
        scope="ai_organization",
        subject=organization_id,
        limit=organization_limit,
        window_seconds=60,
    )
    if not allowed:
        return False, delay, "organization"
    allowed, delay = admit(
        scope="ai_global",
        subject="platform",
        limit=global_limit,
        window_seconds=60,
    )
    return allowed, delay, "global" if not allowed else ""


def admit_provider_start(
    *,
    provider,
    account_id,
    account_limit,
    global_limit,
):
    """Apply account-first then provider-global admission for outbound I/O."""
    provider = str(provider or "provider").strip().lower()[:40]
    allowed, delay = admit(
        scope=f"{provider}_account",
        subject=account_id,
        limit=account_limit,
        window_seconds=60,
    )
    if not allowed:
        return False, delay, "account"
    allowed, delay = admit(
        scope=f"{provider}_global",
        subject="platform",
        limit=global_limit,
        window_seconds=60,
    )
    return allowed, delay, "global" if not allowed else ""
