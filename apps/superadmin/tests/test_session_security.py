from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name


class SuperadminSessionSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            email="security-superadmin@example.com",
            password="StrongSuperadminPassword123!",
            name="Security Superadmin",
        )

    def test_login_rotates_pre_authentication_superadmin_session(self):
        preauth = SessionStore()
        preauth["preauth_marker"] = "preserved"
        preauth.save()
        old_key = preauth.session_key

        cookie_name = get_session_cookie_name("superadmin")
        self.client.cookies[cookie_name] = old_key

        response = self.client.post(
            reverse("superadmin-login"),
            {
                "username": self.user.email,
                "password": "StrongSuperadminPassword123!",
            },
            REMOTE_ADDR="198.51.100.55",
        )

        self.assertEqual(response.status_code, 302)
        new_key = response.cookies[cookie_name].value
        self.assertNotEqual(new_key, old_key)
        self.assertFalse(Session.objects.filter(session_key=old_key).exists())

        rotated = SessionStore(session_key=new_key)
        rotated.load()
        self.assertEqual(rotated.get("_auth_user_id"), str(self.user.pk))
        self.assertEqual(rotated.get("preauth_marker"), "preserved")
