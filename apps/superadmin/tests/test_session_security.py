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


    def test_logout_invalidates_only_superadmin_session(self):
        # Keep a normal Django Admin session alongside the dedicated
        # Superadmin session to verify logout does not cross session areas.
        self.client.force_login(self.user)
        admin_cookie_name = get_session_cookie_name("admin")
        admin_session_key = self.client.cookies[admin_cookie_name].value

        superadmin_session = SessionStore()
        superadmin_session["_auth_user_id"] = str(self.user.pk)
        superadmin_session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        superadmin_session["_auth_user_hash"] = self.user.get_session_auth_hash()
        superadmin_session.save()

        superadmin_cookie_name = get_session_cookie_name("superadmin")
        superadmin_session_key = superadmin_session.session_key
        self.client.cookies[superadmin_cookie_name] = superadmin_session_key

        response = self.client.post(reverse("superadmin-logout"))

        self.assertRedirects(
            response,
            reverse("superadmin-login"),
            fetch_redirect_response=False,
        )
        self.assertFalse(
            Session.objects.filter(session_key=superadmin_session_key).exists()
        )
        self.assertEqual(
            response.cookies[superadmin_cookie_name]["max-age"],
            0,
        )
        self.assertEqual(
            self.client.cookies[admin_cookie_name].value,
            admin_session_key,
        )
        self.assertTrue(
            Session.objects.filter(session_key=admin_session_key).exists()
        )

    def test_logout_rejects_get_requests(self):
        response = self.client.get(reverse("superadmin-logout"))
        self.assertEqual(response.status_code, 405)
