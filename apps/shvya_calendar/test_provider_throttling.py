from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.shvya_calendar.google import GoogleCalendarError, _google_error
from apps.shvya_calendar.tasks import _google_retry_delay


class GoogleCalendarThrottleTests(SimpleTestCase):
    def test_rate_limit_is_retryable_and_preserves_retry_after(self):
        response = SimpleNamespace(
            status_code=429,
            headers={"Retry-After": "31"},
        )
        error = _google_error(
            response,
            "Google Calendar request failed (429).",
        )

        self.assertIsInstance(error, GoogleCalendarError)
        self.assertTrue(error.transient)
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.retry_after, 31)

    def test_retry_delay_uses_provider_backoff_with_bounded_jitter(self):
        countdown = _google_retry_delay(
            booking_id="booking-1",
            retries=2,
            retry_after=31,
        )
        self.assertGreaterEqual(countdown, 31)
        self.assertLessEqual(countdown, 37)
