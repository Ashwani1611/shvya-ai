from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.integrations.models import EmailConfiguration
from apps.integrations.services.email import (
    EmailConfigurationError,
    PinnedEmailBackend,
    build_email_backend,
    send_organization_email,
    test_email_configuration as verify_email_configuration,
    validate_smtp_host,
)
from apps.integrations.services.public_network import ResolvedPublicTarget
from apps.organizations.models import Organization


class EmailConfigurationModelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Email Test Org")

    def _configuration(self, **overrides):
        values = {
            "organization": self.organization,
            "provider": EmailConfiguration.Provider.GMAIL,
            "email_address": "sales@example.com",
            "sender_name": "Sales Team",
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_security": EmailConfiguration.Security.STARTTLS,
            "smtp_username": "sales@example.com",
        }
        values.update(overrides)
        return EmailConfiguration.objects.create(**values)

    def test_password_is_encrypted_and_can_be_recovered(self):
        configuration = self._configuration()
        configuration.set_password("app-password-value")
        configuration.save()

        self.assertNotEqual(
            configuration.encrypted_password,
            "app-password-value",
        )
        self.assertTrue(configuration.has_password)
        self.assertEqual(
            configuration.get_password(),
            "app-password-value",
        )

    def test_only_one_configuration_is_allowed_per_organization(self):
        self._configuration()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._configuration(email_address="second@example.com")

    def test_connected_requires_successful_test_enabled_and_password(self):
        configuration = self._configuration(is_enabled=True)
        self.assertFalse(configuration.is_connected)

        configuration.set_password("app-password-value")
        configuration.last_test_status = EmailConfiguration.TestStatus.SUCCESS
        configuration.save()

        self.assertTrue(configuration.is_connected)


class EmailConfigurationServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Email Service Org")
        self.configuration = EmailConfiguration.objects.create(
            organization=self.organization,
            provider=EmailConfiguration.Provider.GMAIL,
            email_address="sales@example.com",
            sender_name="Sales Team",
            reply_to_email="replies@example.com",
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_security=EmailConfiguration.Security.STARTTLS,
            smtp_username="sales@example.com",
            is_enabled=True,
            last_test_status=EmailConfiguration.TestStatus.SUCCESS,
        )
        self.configuration.set_password("app-password-value")
        self.configuration.save()

    def test_smtp_host_validation_rejects_local_targets(self):
        self.assertEqual(
            validate_smtp_host("smtp.gmail.com"),
            "smtp.gmail.com",
        )

        for value in (
            "localhost",
            "smtp.localhost",
            "127.0.0.1",
            "10.0.0.1",
            "http://smtp.example.com",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_smtp_host(value)

    @patch("apps.integrations.services.email.assert_public_smtp_target")
    @patch("apps.integrations.services.email.PinnedEmailBackend")
    def test_connection_test_uses_dns_pinned_smtp_backend(
        self,
        email_backend_class,
        public_target,
    ):
        public_target.return_value = ResolvedPublicTarget(
            hostname="smtp.gmail.com",
            port=587,
            connect_ip="93.184.216.34",
            addresses=("93.184.216.34",),
        )
        backend = Mock()
        backend.open.return_value = True
        email_backend_class.return_value = backend

        verify_email_configuration(self.configuration)

        public_target.assert_called_once_with("smtp.gmail.com", 587)
        email_backend_class.assert_called_once_with(
            connect_ip="93.184.216.34",
            host="smtp.gmail.com",
            port=587,
            username="sales@example.com",
            password="app-password-value",
            use_tls=True,
            use_ssl=False,
            timeout=15,
            fail_silently=False,
        )
        backend.open.assert_called_once_with()
        backend.close.assert_called_once_with()

    @patch("apps.integrations.services.email.EmailMultiAlternatives")
    @patch("apps.integrations.services.email.build_email_backend")
    def test_send_organization_email_uses_connected_account(
        self,
        build_backend,
        message_class,
    ):
        backend = Mock()
        build_backend.return_value = backend
        message = Mock()
        message.send.return_value = 1
        message_class.return_value = message

        result = send_organization_email(
            organization=self.organization,
            to="lead@example.com",
            subject="Follow-up",
            text_body="Hello",
            html_body="<p>Hello</p>",
        )

        self.assertEqual(result, 1)
        build_backend.assert_called_once_with(self.configuration)
        message_class.assert_called_once()
        kwargs = message_class.call_args.kwargs
        self.assertEqual(kwargs["to"], ["lead@example.com"])
        self.assertEqual(kwargs["reply_to"], ["replies@example.com"])
        self.assertIn("sales@example.com", kwargs["from_email"])
        self.assertEqual(kwargs["connection"], backend)
        message.attach_alternative.assert_called_once_with(
            "<p>Hello</p>",
            "text/html",
        )
        message.send.assert_called_once_with(fail_silently=False)

    @patch("apps.integrations.services.email.smtplib.SMTP")
    def test_starttls_backend_connects_to_pinned_ip_and_verifies_hostname(
        self,
        smtp_class,
    ):
        connection = Mock()
        smtp_class.return_value = connection
        backend = PinnedEmailBackend(
            connect_ip="93.184.216.34",
            host="smtp.example.com",
            port=587,
            username="user@example.com",
            password="secret",
            use_tls=True,
            use_ssl=False,
            timeout=15,
            fail_silently=False,
        )

        self.assertTrue(backend.open())

        connection.connect.assert_called_once_with("93.184.216.34", 587)
        self.assertEqual(connection._host, "smtp.example.com")
        connection.starttls.assert_called_once_with(context=backend.ssl_context)
        connection.login.assert_called_once_with("user@example.com", "secret")

    @patch("apps.integrations.services.email.assert_public_smtp_target")
    def test_build_backend_keeps_hostname_but_pins_resolved_ip(self, public_target):
        public_target.return_value = ResolvedPublicTarget(
            hostname="smtp.gmail.com",
            port=587,
            connect_ip="93.184.216.34",
            addresses=("93.184.216.34",),
        )

        backend = build_email_backend(self.configuration)

        self.assertIsInstance(backend, PinnedEmailBackend)
        self.assertEqual(backend.connect_ip, "93.184.216.34")
        self.assertEqual(backend.host, "smtp.gmail.com")

    def test_send_requires_connected_account(self):
        self.configuration.is_enabled = False
        self.configuration.save(update_fields=["is_enabled"])

        with self.assertRaises(EmailConfigurationError):
            send_organization_email(
                organization=self.organization,
                to="lead@example.com",
                subject="Follow-up",
                text_body="Hello",
            )
