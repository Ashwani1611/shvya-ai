from datetime import timedelta
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from apps.accounts.models import OneTimeLoginToken, User
from apps.accounts.session_utils import get_session_cookie_name
from apps.accounts.views import ThrottledTokenObtainPairView
from apps.organizations.models import Organization


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class AuthenticationSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.organization = Organization.objects.create(
            name="Authentication Security Org",
        )
        self.user = User.objects.create_user(
            email="security-login@example.com",
            password="StrongTestPassword123!",
            name="Security Login Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )

    def tearDown(self):
        cache.clear()

    def _preauth_crm_session(self):
        session = SessionStore()
        session["preauth_marker"] = "preserved"
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )
        return session.session_key

    def _one_time_token(self):
        return OneTimeLoginToken.create_token(
            user=self.user,
            organization=self.organization,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    def test_crm_password_login_rotates_existing_session_key(self):
        old_key = self._preauth_crm_session()

        response = self.client.post(
            reverse("crm-login"),
            {
                "email": self.user.email,
                "password": "StrongTestPassword123!",
            },
            REMOTE_ADDR="198.51.100.10",
        )

        self.assertEqual(response.status_code, 302)
        cookie_name = get_session_cookie_name("dashboard")
        new_key = response.cookies[cookie_name].value
        self.assertNotEqual(new_key, old_key)
        self.assertFalse(Session.objects.filter(session_key=old_key).exists())

        rotated = SessionStore(session_key=new_key)
        rotated.load()
        self.assertEqual(rotated.get("_auth_user_id"), str(self.user.pk))
        self.assertEqual(rotated.get("preauth_marker"), "preserved")

    def test_crm_login_account_failure_limit_applies_across_source_ips(self):
        for index in range(10):
            response = self.client.post(
                reverse("crm-login"),
                {
                    "email": self.user.email,
                    "password": "wrong-password",
                },
                REMOTE_ADDR=f"198.51.100.{index + 20}",
            )
            self.assertNotEqual(response.status_code, 429)

        blocked = self.client.post(
            reverse("crm-login"),
            {
                "email": self.user.email,
                "password": "StrongTestPassword123!",
            },
            REMOTE_ADDR="203.0.113.77",
        )
        self.assertEqual(blocked.status_code, 429)

        cache.clear()
        allowed = self.client.post(
            reverse("crm-login"),
            {
                "email": self.user.email,
                "password": "StrongTestPassword123!",
            },
            REMOTE_ADDR="203.0.113.78",
        )
        self.assertEqual(allowed.status_code, 302)

    def test_jwt_route_uses_hardened_login_view(self):
        match = resolve("/api/v1/auth/token/")
        self.assertIs(match.func.view_class, ThrottledTokenObtainPairView)

    def test_jwt_account_failure_limit_applies_across_source_ips(self):
        url = reverse("token_obtain_pair")
        for index in range(10):
            response = self.client.post(
                url,
                {
                    "email": self.user.email,
                    "password": "wrong-password",
                },
                content_type="application/json",
                REMOTE_ADDR=f"198.51.100.{index + 60}",
            )
            self.assertNotEqual(response.status_code, 429)

        blocked = self.client.post(
            url,
            {
                "email": self.user.email,
                "password": "StrongTestPassword123!",
            },
            content_type="application/json",
            REMOTE_ADDR="203.0.113.88",
        )
        self.assertEqual(blocked.status_code, 429)

        cache.clear()
        allowed = self.client.post(
            url,
            {
                "email": self.user.email,
                "password": "StrongTestPassword123!",
            },
            content_type="application/json",
            REMOTE_ADDR="203.0.113.89",
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("access", allowed.json())
        self.assertIn("refresh", allowed.json())

    def test_one_time_login_consumes_token_and_rotates_session(self):
        token, raw_token = self._one_time_token()
        old_key = self._preauth_crm_session()

        response = self.client.get(
            reverse("one-time-login"),
            {"token": raw_token},
        )

        self.assertEqual(response.status_code, 302)
        token.refresh_from_db()
        self.assertIsNotNone(token.used_at)

        cookie_name = get_session_cookie_name("dashboard")
        new_key = response.cookies[cookie_name].value
        self.assertNotEqual(new_key, old_key)
        self.assertFalse(Session.objects.filter(session_key=old_key).exists())

        replay = self.client.get(
            reverse("one-time-login"),
            {"token": raw_token},
        )
        self.assertEqual(replay.status_code, 400)

    def test_one_time_login_rejects_inactive_organization_without_consuming(self):
        token, raw_token = self._one_time_token()
        self.organization.is_active = False
        self.organization.save(update_fields=["is_active", "updated_at"])

        response = self.client.get(
            reverse("one-time-login"),
            {"token": raw_token},
        )

        self.assertEqual(response.status_code, 403)
        token.refresh_from_db()
        self.assertIsNone(token.used_at)

    def test_one_time_token_is_consumed_before_session_authentication(self):
        token, raw_token = self._one_time_token()

        with patch(
            "apps.accounts.views_flat.set_authenticated_user",
            side_effect=RuntimeError("simulated session failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.get(
                    reverse("one-time-login"),
                    {"token": raw_token},
                )

        token.refresh_from_db()
        self.assertIsNotNone(token.used_at)
