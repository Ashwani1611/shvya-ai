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

logger = logging.getLogger(__name__)


class EmailConfigurationError(RuntimeError):
    """Raised when an organization email configuration cannot be used."""


def _validate_public_ip(ip_value):
    ip_obj = ipaddress.ip_address(ip_value)
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        raise ValidationError(
            "SMTP host must resolve to a public internet address."
        )


def validate_smtp_host(value):
    """Validate stored SMTP host text without requiring DNS to be live yet."""
    host = str(value or "").strip().lower()
    if not host:
        raise ValidationError("SMTP host is required.")

    if "://" in host or "/" in host or "@" in host:
        raise ValidationError(
            "Enter only the SMTP hostname, for example smtp.gmail.com."
        )

    if host == "localhost" or host.endswith(".localhost"):
        raise ValidationError("SMTP host cannot target localhost.")

    try:
        _validate_public_ip(host)
    except ValueError:
        pass

    return host


def assert_public_smtp_target(host, port):
    """Resolve SMTP immediately before use to reduce SSRF/DNS-rebinding risk."""
    host = validate_smtp_host(host)
    try:
        addresses = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise EmailConfigurationError(
            "SMTP hostname could not be resolved."
        ) from exc

    if not addresses:
        raise EmailConfigurationError("SMTP hostname could not be resolved.")

    try:
        for address in addresses:
            _validate_public_ip(address[4][0])
    except ValidationError as exc:
        raise EmailConfigurationError(exc.message) from exc

    return host


def build_email_backend(configuration: EmailConfiguration) -> EmailBackend:
    """Create a Django SMTP backend from one organization configuration."""
    password = configuration.get_password()
    if not password:
        raise EmailConfigurationError(
            "Add an email password or app password before testing the connection."
        )

    if not configuration.smtp_host or not configuration.smtp_username:
        raise EmailConfigurationError(
            "SMTP host and username are required before testing the connection."
        )

    use_tls = (
        configuration.smtp_security == EmailConfiguration.Security.STARTTLS
    )
    use_ssl = configuration.smtp_security == EmailConfiguration.Security.SSL

    return EmailBackend(
        host=configuration.smtp_host,
        port=configuration.smtp_port,
        username=configuration.smtp_username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        timeout=15,
        fail_silently=False,
    )


def test_email_configuration(configuration: EmailConfiguration) -> None:
    """Authenticate to the configured SMTP server without sending a message."""
    assert_public_smtp_target(
        configuration.smtp_host,
        configuration.smtp_port,
    )
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
    """Send through the organization's connected mailbox.

    Email automations and future email-event handlers should call this function
    instead of reading credentials directly.
    """
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

    assert_public_smtp_target(
        configuration.smtp_host,
        configuration.smtp_port,
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
