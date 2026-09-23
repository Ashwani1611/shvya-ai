from django.test import SimpleTestCase

from apps.ai_engagement.services.ai_provider import (
    AIProviderTransientError,
    provider_retry_countdown,
)


class AIProviderRetryTimingTests(SimpleTestCase):
    def test_provider_retry_after_is_honored_with_bounded_jitter(self):
        error = AIProviderTransientError(
            "rate limited",
            retry_after=41,
        )
        countdown = provider_retry_countdown(
            error,
            identifier="lead-1",
            retries=2,
            default=30,
        )
        self.assertGreaterEqual(countdown, 41)
        self.assertLessEqual(countdown, 47)

    def test_retry_backoff_is_bounded_without_provider_hint(self):
        error = AIProviderTransientError("temporary network failure")
        countdown = provider_retry_countdown(
            error,
            identifier="lead-2",
            retries=20,
            default=30,
            maximum=300,
        )
        self.assertGreaterEqual(countdown, 300)
        self.assertLessEqual(countdown, 306)
