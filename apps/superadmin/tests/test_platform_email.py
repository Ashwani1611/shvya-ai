from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User, SignupVerificationDelivery
from apps.accounts.session_utils import set_authenticated_user
from apps.accounts.signup_delivery import deliver_signup_verification
from apps.integrations.services.email import EmailConfigurationError
from apps.organizations.models import Organization
from apps.superadmin.models import PlatformEmailConfiguration, AuditLog
from apps.superadmin.platform_email import platform_verification_mail_options


class PlatformEmailTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email="platform@example.com", password="Test-Password-123")
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key
        self.url = reverse("superadmin-platform-email")
        self.payload = dict(email_address="platform@example.com", sender_name="SHVYA AI", smtp_host="smtp.gmail.com", smtp_port=587, smtp_security="starttls", smtp_username="platform@example.com", password="test-app-password", action="connect")

    @patch("apps.superadmin.platform_email.test_email_configuration")
    def test_connect_encrypts_password_and_never_displays_it(self, test_connection):
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, 302)
        configuration = PlatformEmailConfiguration.objects.get()
        self.assertTrue(configuration.is_enabled)
        self.assertNotIn(self.payload["password"], configuration.encrypted_password)
        self.assertEqual(configuration.as_smtp_configuration().get_password(), self.payload["password"])
        test_connection.assert_called_once()
        page = self.client.get(self.url)
        self.assertContains(page, "Connected")
        self.assertNotContains(page, self.payload["password"])
        self.assertNotContains(page, configuration.encrypted_password)
        self.assertNotIn(self.payload["password"], str(AuditLog.objects.get().metadata))
        self.client.post(self.url, {**self.payload, "password": "", "action": "save"})
        configuration.refresh_from_db()
        self.assertFalse(configuration.is_enabled)
        self.assertEqual(configuration.as_smtp_configuration().get_password(), self.payload["password"])

    @patch("apps.superadmin.platform_email.test_email_configuration", side_effect=RuntimeError("secret-provider-response"))
    def test_failed_connection_cannot_be_enabled_or_leak_provider_response(self, _test):
        self.client.post(self.url, self.payload)
        configuration = PlatformEmailConfiguration.objects.get()
        self.assertFalse(configuration.is_enabled)
        self.assertEqual(configuration.last_error, "RuntimeError")
        self.assertNotContains(self.client.get(self.url), "secret-provider-response")
        with self.assertRaises(EmailConfigurationError):
            platform_verification_mail_options()

    def test_invalid_settings_do_not_create_configuration(self):
        for changes in [{"smtp_host": "127.0.0.1"}, {"smtp_security": "none"}, {"smtp_port": 70000}, {"password": ""}]:
            with self.subTest(changes=changes):
                response = self.client.post(self.url, {**self.payload, **changes})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(PlatformEmailConfiguration.objects.exists())

    def test_anonymous_and_organization_admin_cannot_change_platform_sender(self):
        client = Client()
        self.assertEqual(client.post(self.url, self.payload).status_code, 302)
        org = Organization.objects.create(name="Tenant")
        user = User.objects.create_user(email="tenant@example.com", organization=org, role="admin")
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        client.cookies["shvya_superadmin_sessionid"] = session.session_key
        self.assertEqual(client.post(self.url, self.payload).status_code, 302)
        self.assertFalse(PlatformEmailConfiguration.objects.exists())

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.cookies = self.client.cookies
        self.assertEqual(client.post(self.url, self.payload).status_code, 403)

    @patch("apps.superadmin.platform_email.test_email_configuration")
    @patch("apps.superadmin.platform_email.build_email_backend")
    @patch("apps.accounts.signup_delivery.send_mail", return_value=1)
    def test_signup_verification_uses_platform_sender_and_signup_recipient(self, send_mail, backend, _test):
        self.client.post(self.url, self.payload)
        org = Organization.objects.create(name="Pending", is_active=False)
        user = User.objects.create_user(email="signup@example.com", organization=org, is_active=False)
        delivery = SignupVerificationDelivery.objects.create(user=user, email=user.email, verification_endpoint="https://example.com/signup/verify-email/")
        deliver_signup_verification(delivery.pk)
        self.assertEqual(send_mail.call_args.kwargs["recipient_list"], [user.email])
        self.assertEqual(send_mail.call_args.kwargs["from_email"], "SHVYA AI <platform@example.com>")
        self.assertIs(send_mail.call_args.kwargs["connection"], backend.return_value)
        self.client.post(self.url, {"action": "disconnect"})
        SignupVerificationDelivery.objects.filter(pk=delivery.pk).update(delivered_at=None, next_attempt_at=timezone.now())
        deliver_signup_verification(delivery.pk)
        self.assertEqual(send_mail.call_count, 1)
        delivery.refresh_from_db()
        self.assertIsNone(delivery.delivered_at)
        self.assertEqual(delivery.error_type, "EmailConfigurationError")
