from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class SignupSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("crm-signup")
        self.remote_addr = "198.51.100.40"

    def tearDown(self):
        cache.clear()

    def _invalid_signup(self):
        return self.client.post(
            self.url,
            {
                "name": "",
                "email": "",
                "phone": "",
                "company_name": "",
                "password": "",
            },
            REMOTE_ADDR=self.remote_addr,
        )

    def test_signup_page_gets_do_not_consume_submission_limit(self):
        for _ in range(25):
            response = self.client.get(
                self.url,
                REMOTE_ADDR=self.remote_addr,
            )
            self.assertEqual(response.status_code, 200)

        response = self._invalid_signup()
        self.assertEqual(response.status_code, 400)

    def test_signup_submissions_are_rate_limited_per_source_ip(self):
        for _ in range(10):
            response = self._invalid_signup()
            self.assertEqual(response.status_code, 400)

        blocked = self._invalid_signup()
        self.assertEqual(blocked.status_code, 429)
        self.assertContains(
            blocked,
            "Too many attempts",
            status_code=429,
        )
