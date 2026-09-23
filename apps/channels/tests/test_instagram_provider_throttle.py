from types import SimpleNamespace

from django.test import SimpleTestCase

from services.channels.instagram_service import InstagramAPIError, _raise_for_meta


class InstagramProviderThrottleTests(SimpleTestCase):
    def test_rate_limit_is_transient_and_preserves_retry_after(self):
        response = SimpleNamespace(
            ok=False,
            status_code=429,
            text='{"error":{"code":4,"message":"Rate limit"}}',
            headers={"Retry-After": "37"},
            json=lambda: {
                "error": {
                    "code": 4,
                    "message": "Rate limit",
                    "is_transient": False,
                }
            },
        )

        with self.assertRaises(InstagramAPIError) as raised:
            _raise_for_meta(response, "Instagram API request failed")

        self.assertTrue(raised.exception.transient)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.retry_after, 37)
