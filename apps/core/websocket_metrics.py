"""Shared Redis-backed WebSocket presence with crash-safe expiry."""

from __future__ import annotations

import time

from django.conf import settings
import redis

from apps.core.observability import increment


def _client():
    return redis.Redis.from_url(settings.CHANNEL_LAYER_REDIS_URL)


def touch(kind, channel_name, organization_id):
    try:
        member = f"{organization_id}:{channel_name}"
        client = _client()
        key = f"shvya:websockets:{kind}"
        client.zadd(key, {member: time.time()})
        client.expire(key, 300)
    except Exception:
        # Presence telemetry must never terminate a customer socket when the
        # metrics Redis path is unavailable.
        return


def remove(kind, channel_name, organization_id):
    try:
        _client().zrem(
            f"shvya:websockets:{kind}",
            f"{organization_id}:{channel_name}",
        )
    except Exception:
        pass
    increment("websocket.disconnects", labels={"kind": kind})
