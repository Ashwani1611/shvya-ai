from __future__ import annotations

import ipaddress
import logging
import smtplib
import socket
import ssl
from email.utils import formataddr

from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend

from apps.integrations.models import EmailConfiguration
from apps.integrations.services.public_network import (
    PublicNetworkTargetError,
    ResolvedPublicTarget,
    resolve_public_target,
)

logger = logging.getLogger(__name__)


class EmailConfigurationError(RuntimeError):
    """Raised when an organization email configuration cannot be used."""


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    """SMTPS socket pinned to an IP while TLS verifies the configured hostname."""

    def __init__(
        self,
        *,
        hostname: str,
        connect_ip: str,
        port: int,
        timeout: float | None,
        context: ssl.SSLContext,
    ):
        self._shvya_hostname = hostname
        self._shvya_connect_ip = connect_ip
        super().__init__(
            host="",
            port=0,
            timeout=timeout,
            context=context,
        )
        self.connect(connect_ip, port)
        self._host = hostname

    def _get_socket(self, host, port, timeout):
        if self.debuglevel > 0:
            self._print_debug("connect:", (self._shvya_connect_ip, port))
        raw_socket = socket.create_connection(
            (self._shvya_connect_ip, port),
            timeout,
            self.source_address,
        )
        try:
            return self.context.wrap_socket(
                raw_socket,
                server_hostname=self._shvya_hostname,
            )
        except Exception:
            raw_socket.close()
            raise


class PinnedEmailBackend(EmailBackend):
    """Django SMTP backend that never re-resolves the validated remote host."""

    def __init__(self, *, connect_ip: str, **kwargs):
        self.connect_ip = connect_ip
        super().__init__(**kwargs)

    def open(self):
        if self.connection:
            return False

        try:
            if self.use_ssl:
                self.connection = _PinnedSMTPSSL(
                    hostname=self.host,
                    connect_ip=self.connect_ip,
                    port=self.port,
                    timeout=self.timeout,
                    context=self.ssl_context,
                )
            else:
                self.connection = smtplib.SMTP(timeout=self.timeout)
                self.connection.connect(self.connect_ip, self.port)
                # smtplib STARTTLS uses _host for SNI and certificate hostname
                # verification. Restore the configured name after connecting to
                # the pinned IP so DNS cannot change the destination.
                self.connection._host = self.host
                if self.use_tls:
                    self.connection.starttls(context=self.ssl_context)

            if self.username and self.password:
                self.connection.login(self.username, self.password)
            return True
        except OSError:
            self.connection = None
            if not self.fail_silently:
                raise
            return None


def _validate_literal_public_ip(hostname: str) -> None:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValidationError(
            "SMTP host must target a public internet address."
        )


def validate_smtp_host(value):
    """Validate stored SMTP host text without requiring DNS to be live yet."""
    host = str(value or "").strip().rstrip(".").lower()
    if not host:
        raise ValidationError("SMTP host is required.")

    if "://" in host or "/" in host or "@" in host:
        raise ValidationError(
            "Enter only the SMTP hostname, for example smtp.gmail.com."
        )

    if host == "localhost" or host.endswith(".localhost"):
        raise ValidationError("SMTP host cannot target localhost.")

    _validate_literal_public_ip(host)
    return host


def assert_public_smtp_target(host, port) -> ResolvedPublicTarget:
    """Resolve once and return the exact public IP the SMTP socket must use."""
    host = validate_smtp_host(host)
    try:
        return resolve_public_target(host, port)
    except PublicNetworkTargetError as exc:
        raise EmailConfigurationError(str(exc)) from exc


def build_email_backend(configuration: EmailConfiguration) -> EmailBackend:
    """Create a DNS-pinned Django SMTP backend from one organization config."""
    password = configuration.get_password()
    if not password:
        raise EmailConfigurationError(
            "Add an email password or app password before testing the connection."
        )

    if not configuration.smtp_host or not configuration.smtp_username:
        raise EmailConfigurationError(
            "SMTP host and username are required before testing the connection."
        )

    target = assert_public_smtp_target(
        configuration.smtp_host,
        configuration.smtp_port,
    )

    use_tls = (
        configuration.smtp_security == EmailConfiguration.Security.STARTTLS
    )
    use_ssl = configuration.smtp_security == EmailConfiguration.Security.SSL

    return PinnedEmailBackend(
        connect_ip=target.connect_ip,
        host=target.hostname,
        port=target.port,
        username=configuration.smtp_username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        timeout=15,
        fail_silently=False,
    )


def test_email_configuration(configuration: EmailConfiguration) -> None:
    """Authenticate to the configured SMTP server without sending a message."""
    backend = build_email_backend(configuration)

    try:
        opened = backend.open()
        if opened is False and backend.connection is None:
            raise EmailConfigurationError(
                "The SMTP server did not accept the connection."
            )
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailConfigurationError(
            "Authentication failed. Check the SMTP username and password/app password."
        ) from exc
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected) as exc:
        raise EmailConfigurationError(
            "Could not connect to the SMTP server. Check the host, port and security."
        ) from exc
    except (ssl.SSLError, OSError) as exc:
        raise EmailConfigurationError(
            "A secure SMTP connection could not be established. Check the host, port and security."
        ) from exc
    except smtplib.SMTPException as exc:
        raise EmailConfigurationError(
            "The SMTP server rejected the connection. Check the provider settings and try again."
        ) from exc
    finally:
        try:
            backend.close()
        except (smtplib.SMTPException, OSError):
            logger.debug("SMTP backend close failed after connection test.", exc_info=True)


def send_organization_email(
    *,
    organization,
    to,
    subject,
    text_body,
    html_body=None,
    reply_to=None,
    headers=None,
) -> int:
    """Send through the organization's connected DNS-pinned mailbox."""
    try:
        configuration = EmailConfiguration.objects.get(
            organization=organization,
            is_enabled=True,
            last_test_status=EmailConfiguration.TestStatus.SUCCESS,
        )
    except EmailConfiguration.DoesNotExist as exc:
        raise EmailConfigurationError(
            "No connected email account is available for this organization."
        ) from exc

    recipients = [to] if isinstance(to, str) else list(to or [])
    if not recipients:
        raise EmailConfigurationError(
            "At least one recipient email address is required."
        )

    backend = build_email_backend(configuration)
    from_email = (
        formataddr((configuration.sender_name, configuration.email_address))
        if configuration.sender_name
        else configuration.email_address
    )

    configured_reply_to = (
        configuration.reply_to_email or configuration.email_address
    )
    reply_to_addresses = reply_to or [configured_reply_to]
    if isinstance(reply_to_addresses, str):
        reply_to_addresses = [reply_to_addresses]

    message = EmailMultiAlternatives(
        subject=str(subject or ""),
        body=str(text_body or ""),
        from_email=from_email,
        to=recipients,
        reply_to=list(reply_to_addresses),
        headers=headers or None,
        connection=backend,
    )
    if html_body:
        message.attach_alternative(html_body, "text/html")

    try:
        return message.send(fail_silently=False)
    except Exception as exc:
        logger.exception(
            "Configured email delivery failed for organization %s.",
            organization.pk,
        )
        raise EmailConfigurationError(
            "The email could not be sent through the connected mailbox."
        ) from exc
