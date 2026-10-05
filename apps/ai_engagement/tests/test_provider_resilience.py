from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services import provider_resilience


class _MemoryCache:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def add(self, key, value, timeout=None):
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def incr(self, key, delta=1):
        self.values[key] = int(self.values[key]) + int(delta)
        return self.values[key]

    def set(self, key, value, timeout=None):
        self.values[key] = value
        return True

    def delete_many(self, keys):
        for key in keys:
            self.values.pop(key, None)


class ProviderCircuitBreakerTests(SimpleTestCase):
    def test_transient_failures_open_and_success_resets_breaker(self):
        fake = _MemoryCache()
        with (
            patch.object(provider_resilience, "cache", fake),
            patch.dict(
                "os.environ",
                {
                    "OPENAI_CIRCUIT_BREAKER_FAILURE_THRESHOLD": "2",
                    "OPENAI_CIRCUIT_BREAKER_WINDOW_SECONDS": "60",
                    "OPENAI_CIRCUIT_BREAKER_COOLDOWN_SECONDS": "45",
                },
                clear=False,
            ),
        ):
            self.assertFalse(provider_resilience.record_transient_failure("model-a"))
            self.assertTrue(provider_resilience.record_transient_failure("model-a"))
            self.assertTrue(provider_resilience.is_open("model-a"))

            provider_resilience.record_success("model-a")
            self.assertFalse(provider_resilience.is_open("model-a"))

    def test_cache_failure_never_blocks_provider(self):
        broken = _MemoryCache()
        broken.get = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("redis down"))
        with patch.object(provider_resilience, "cache", broken):
            self.assertFalse(provider_resilience.is_open("model-a"))
