from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient


class WhatsAppProviderRateLimitTests(SimpleTestCase):
    @patch("apps.channels.providers.whatsapp.requests.post")
    def test_meta_retry_after_is_preserved_on_rate_limit(self, post):
        post.return_value = SimpleNamespace(
            ok=False,
            status_code=429,
            text='{"error":{"code":130429}}',
            headers={"Retry-After": "42"},
        )
        client = WhatsAppClient(
            phone_number_id="phone-id",
            access_token="token",
        )

        with self.assertRaises(WhatsAppAPIError) as raised:
            client.send_text_message(
                to="919000000001",
                body="rate-limit test",
            )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.retry_after, 42)
