import socket
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.integrations.services.email import (
    EmailConfigurationError,
    assert_public_smtp_target,
)
from apps.integrations.services.public_network import (
    PublicNetworkTargetError,
    resolve_public_target,
)
from apps.integrations.services.webhook import (
    WebhookHTTPResponse,
    assert_public_webhook_target,
    send_webhook_request,
)


class PublicNetworkResolutionTests(SimpleTestCase):
    @patch("apps.integrations.services.public_network.socket.getaddrinfo")
    def test_public_hostname_resolves_to_pinned_public_ip(self, getaddrinfo):
        getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.35", 443),
            ),
        ]

        target = resolve_public_target("hooks.example.com", 443)

        self.assertEqual(target.hostname, "hooks.example.com")
        self.assertEqual(target.connect_ip, "93.184.216.34")
        self.assertEqual(
            target.addresses,
            ("93.184.216.34", "93.184.216.35"),
        )

    @patch("apps.integrations.services.public_network.socket.getaddrinfo")
    def test_mixed_public_private_dns_answer_fails_closed(self, getaddrinfo):
        getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            ),
        ]

        with self.assertRaises(PublicNetworkTargetError):
            resolve_public_target("rebind.example.com", 443)

    def test_literal_private_targets_fail_without_dns(self):
        for hostname in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            with self.subTest(hostname=hostname):
                with self.assertRaises(PublicNetworkTargetError):
                    resolve_public_target(hostname, 443)


class WebhookPinnedTransportTests(SimpleTestCase):
    @patch("apps.integrations.services.public_network.socket.getaddrinfo")
    def test_webhook_target_preserves_hostname_and_pins_ip(self, getaddrinfo):
        getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]

        target = assert_public_webhook_target(
            "https://hooks.example.com/events?source=shvya"
        )

        self.assertEqual(target.hostname, "hooks.example.com")
        self.assertEqual(target.connect_ip, "93.184.216.34")
        self.assertEqual(target.host_header, "hooks.example.com")
        self.assertEqual(target.request_target, "/events?source=shvya")

    @patch("apps.integrations.services.webhook._PinnedHTTPSConnection")
    def test_webhook_request_connects_with_validated_ip_and_original_host(
        self,
        connection_class,
    ):
        connection = Mock()
        response = Mock()
        response.status = 202
        response.getheader.side_effect = lambda name, default="": (
            "2" if name == "Content-Length" else default
        )
        response.read.return_value = b"ok"
        connection.getresponse.return_value = response
        connection_class.return_value = connection

        target = Mock(
            hostname="hooks.example.com",
            connect_ip="93.184.216.34",
            port=443,
            request_target="/events",
            host_header="hooks.example.com",
        )

        result = send_webhook_request(
            target,
            payload={"lead_id": "lead-1"},
            headers={"X-Test": "1"},
            timeout=10,
        )

        self.assertEqual(
            result,
            WebhookHTTPResponse(status_code=202, text="ok"),
        )
        connection_class.assert_called_once_with(
            hostname="hooks.example.com",
            connect_ip="93.184.216.34",
            port=443,
            timeout=10,
        )
        request = connection.request.call_args
        self.assertEqual(request.args[0], "POST")
        self.assertEqual(request.args[1], "/events")
        self.assertEqual(
            request.kwargs["headers"]["Host"],
            "hooks.example.com",
        )
        self.assertEqual(
            request.kwargs["headers"]["Accept-Encoding"],
            "identity",
        )

    @patch("apps.integrations.services.public_network.socket.getaddrinfo")
    def test_webhook_mixed_dns_answer_is_rejected(self, getaddrinfo):
        getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.5", 443),
            ),
        ]

        with self.assertRaises(ValidationError):
            assert_public_webhook_target("https://hooks.example.com/events")


class SMTPPinnedTargetTests(SimpleTestCase):
    @patch("apps.integrations.services.public_network.socket.getaddrinfo")
    def test_smtp_target_returns_pinned_public_ip(self, getaddrinfo):
        getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 587),
            )
        ]

        target = assert_public_smtp_target("smtp.example.com", 587)

        self.assertEqual(target.hostname, "smtp.example.com")
        self.assertEqual(target.connect_ip, "93.184.216.34")

    @patch("apps.integrations.services.public_network.socket.getaddrinfo")
    def test_smtp_mixed_dns_answer_is_rejected(self, getaddrinfo):
        getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 587),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("192.168.1.10", 587),
            ),
        ]

        with self.assertRaises(EmailConfigurationError):
            assert_public_smtp_target("smtp.example.com", 587)
