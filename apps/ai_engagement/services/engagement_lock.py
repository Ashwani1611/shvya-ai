from __future__ import annotations

import uuid

import redis
from django.conf import settings


class EngagementLockError(Exception):
    """Raised when the engagement Redis claim cannot be managed safely."""


class EngagementGenerationLock:
    """Redis claim that prevents duplicate LLM calls for one inbound message.

    The lock is keyed by lead + source inbound message. A successful generation
    intentionally leaves the key until TTL expiry so concurrently queued tasks
    for the same WhatsApp turn cannot consume a second OpenAI call. A failed
    provider attempt releases its claim so Celery retry can try again.
    """

    LOCK_PREFIX = "shvya:ai:engagement:claim"
    LOCK_TTL_SECONDS = 180

    def __init__(self, *, lead_id, source_message_id) -> None:
        self.lead_id = str(lead_id)
        self.source_message_id = str(source_message_id or "unknown")
        self.key = f"{self.LOCK_PREFIX}:{self.lead_id}:{self.source_message_id}"
        self.token = uuid.uuid4().hex
        self.client = None
        self.acquired = False

    def _get_client(self):
        redis_url = getattr(settings, "REDIS_URL", "")
        if not redis_url:
            raise EngagementLockError("REDIS_URL is not configured.")
        try:
            return redis.Redis.from_url(redis_url, decode_responses=True)
        except Exception as exc:
            raise EngagementLockError(
                f"Unable to create engagement Redis client: {exc}"
            ) from exc

    def acquire(self) -> bool:
        if self.acquired:
            return True
        client = self._get_client()
        try:
            acquired = client.set(
                self.key,
                self.token,
                nx=True,
                ex=self.LOCK_TTL_SECONDS,
            )
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            raise EngagementLockError(
                f"Unable to acquire engagement claim: {exc}"
            ) from exc
        if not acquired:
            try:
                client.close()
            except Exception:
                pass
            return False
        self.client = client
        self.acquired = True
        return True

    def finish(self, *, success: bool) -> None:
        """Close the claim; delete it only when generation failed."""
        if not self.acquired:
            return
        client = self.client
        try:
            if client is not None and not success:
                script = """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                    return redis.call('del', KEYS[1])
                else
                    return 0
                end
                """
                client.eval(script, 1, self.key, self.token)
        except Exception as exc:
            raise EngagementLockError(
                f"Unable to finalize engagement claim: {exc}"
            ) from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            self.client = None
            self.acquired = False
