from __future__ import annotations

import uuid

import redis
from django.conf import settings


class SummaryLockError(Exception):
    """
    Raised when the conversation-summary Redis lock cannot
    be safely acquired or released.
    """


class ConversationSummaryLock:
    """
    Redis-backed per-Lead lock for internal conversation-summary
    generation.

    The lock is intentionally implemented directly with Redis
    because acquire/release must use the same raw Redis key and
    token for an atomic compare-and-delete operation.

    Lock scope:

        One lock per Lead.

    Example key:

        shvya:ai:conversation-summary:lock:<lead_id>
    """

    LOCK_PREFIX = "shvya:ai:conversation-summary:lock"

    LOCK_TTL_SECONDS = 300

    def __init__(
        self,
        *,
        lead_id,
    ) -> None:

        self.lead_id = str(
            lead_id
        )

        self.key = (
            f"{self.LOCK_PREFIX}:{self.lead_id}"
        )

        self.token = uuid.uuid4().hex

        self.acquired = False

        self.client = None

    # ========================================================
    # REDIS CLIENT
    # ========================================================

    def _get_client(self):
        """
        Create the Redis client used by this lock.

        The same REDIS_URL already powers SHVYA's Django cache
        and Celery broker infrastructure.
        """

        redis_url = getattr(
            settings,
            "REDIS_URL",
            "",
        )

        if not redis_url:
            raise SummaryLockError(
                "REDIS_URL is not configured."
            )

        try:

            return redis.Redis.from_url(
                redis_url,
                decode_responses=True,
            )

        except Exception as exc:

            raise SummaryLockError(
                f"Unable to create Redis client: {exc}"
            ) from exc

    # ========================================================
    # ACQUIRE
    # ========================================================

    def acquire(self) -> bool:
        """
        Attempt to acquire the per-Lead lock.

        Returns:

            True
                This instance acquired the lock.

            False
                Another worker already owns the lock.
        """

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

            raise SummaryLockError(
                f"Unable to acquire summary lock: {exc}"
            ) from exc

        if not acquired:

            return False

        self.client = client

        self.acquired = True

        return True

    # ========================================================
    # RELEASE
    # ========================================================

    def release(self) -> None:
        """
        Release the lock only when this instance still owns it.

        The Lua script performs:

            GET key
              ↓
            compare token
              ↓
            DELETE key

        as one atomic Redis operation.

        This prevents one worker from accidentally deleting a
        newer worker's lock.
        """

        if not self.acquired:
            return

        if self.client is None:
            self.acquired = False
            return

        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        try:

            self.client.eval(
                script,
                1,
                self.key,
                self.token,
            )

        except Exception as exc:

            raise SummaryLockError(
                f"Unable to release summary lock: {exc}"
            ) from exc

        finally:

            try:
                self.client.close()
            except Exception:
                pass

            self.client = None

            self.acquired = False

    # ========================================================
    # CONTEXT MANAGER
    # ========================================================

    def __enter__(
        self,
    ):
        """
        Context-manager support.

        Returns:

            self
                when the lock is acquired.

            None
                when another worker owns the lock.
        """

        if not self.acquire():
            return None

        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):
        """
        Release the lock when leaving the context.
        """

        self.release()

        return False