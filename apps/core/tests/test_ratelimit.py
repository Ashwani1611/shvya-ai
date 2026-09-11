from django.test import RequestFactory, SimpleTestCase

from apps.core.ratelimit import _client_ip


class RateLimitClientIPTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_direct_public_client_cannot_spoof_forwarded_ip(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="8.8.8.8",
            HTTP_X_REAL_IP="1.2.3.4",
            HTTP_X_FORWARDED_FOR="1.2.3.4",
        )

        self.assertEqual(_client_ip(request), "8.8.8.8")

    def test_private_reverse_proxy_can_supply_real_ip(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="172.18.0.5",
            HTTP_X_REAL_IP="203.0.113.25",
            HTTP_X_FORWARDED_FOR="203.0.113.25",
        )

        self.assertEqual(_client_ip(request), "203.0.113.25")

    def test_private_proxy_falls_back_to_forwarded_for(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="172.18.0.5",
            HTTP_X_FORWARDED_FOR="203.0.113.25, 172.18.0.2",
        )

        self.assertEqual(_client_ip(request), "203.0.113.25")
