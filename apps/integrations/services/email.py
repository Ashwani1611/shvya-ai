from __future__ import annotations

import ipaddress
import logging
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.utils import formataddr

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend

from apps.integrations.models import EmailConfiguration

logger = logging.getLogger(__name__)


class EmailConfigurationError(RuntimeError):
    """Raised when an organization email configuration cannot be used."""


@dataclass(frozen=True)
class ValidatedSMTPTarget:
    hostname: str
    port: int
    connect_ip: str


class _PinnedSMTP(smtplib.SMTP):
    """SMTP client whose TCP destination is a pre-validated public IP."""

    def __init__(self, host="", port=0, *, connect_ip, **kwargs):
        self._connect_ip = connect_ip
        super().__init__(host=host, port=port, **kwargs)

    def _get_socket(self, host, port, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        return socket.create_connection(
            (self._connect_ip, port),
            timeout,
            self.source_address,
        )


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    """SMTPS client pinned to an IP while verifying the configured hostname."""

    def __init__(self, host="", port=0, *, connect_ip, **kwargs):
        self._connect_ip = connect_ip
        super().__init__(host=host, port=port, **kwargs)

    def _get_socket(self, host, port, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        raw_socket = socket.create_connection(
            (self._connect_ip, port),
            timeout,
            self.source_address,
        )
        try:
            context = getattr(self, "context", None) or getattr(self, "_context", None)
            if context is None:  # pragma: no cover - defensive across Python versions
                context = ssl.create_default_context()
            return context.wrap_socket(
                raw_socket,
                server_hostname=self._host,
            )
        except Exception:
            raw_socket.close()
            raise


class PinnedEmailBackend(EmailBackend):
    """Django SMTP backend that never performs a second DNS lookup."""

    def __init__(self, *args, connect_ip, **kwargs):
        self.connect_ip = str(connect_ip)
        super().__init__(*args, **kwargs)

    @property
    def connection_class(self):
        client_class = _PinnedSMTPSSL if self.use_ssl else _PinnedSMTP
        connect_ip = self.connect_ip

        def factory(host="", port=0, **kwargs):
            return client_class(
                host=host,
                port=port,
                connect_ip=connect_ip,
                **kwargs,
            )

        return factory


def _validate_public_ip(ip_value):
    ip_obj = ipaddress.ip_address(ip_value)
    if not ip_obj.is_global:
        raise ValidationError(
            "SMTP host must resolve to a public internet address."
        )


def validate_smtp_host(value):
    """Validate stored SMTP host text without requiring DNS to be live yet."""
    host = str(value or "").strip().lower().rstrip(".")
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
    """Resolve once and bind SMTP use to an exact validated public IP."""
    host = validate_smtp_host(host)
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise EmailConfigurationError("SMTP port is invalid.") from exc

    if not 1 <= port <= 65535:
        raise EmailConfigurationError("SMTP port is invalid.")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [literal]
    else:
        try:
            records = socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise EmailConfigurationError(
                "SMTP hostname could not be resolved."
            ) from exc

        addresses = []
        for record in records:
            try:
                address = ipaddress.ip_address(record[4][0])
            except (IndexError, ValueError):
                continue
            if address not in addresses:
                addresses.append(address)

    if not addresses:
        raise EmailConfigurationError("SMTP hostname could not be resolved.")

    try:
        for address in addresses:
            _validate_public_ip(address)
    except ValidationError as exc:
        raise EmailConfigurationError("; ".join(exc.messages)) from exc

    return ValidatedSMTPTarget(
        hostname=host,
        port=port,
        connect_ip=str(addresses[0]),
    )


def build_email_backend(
    configuration: EmailConfiguration,
    *,
    target: ValidatedSMTPTarget | None = None,
) -> EmailBackend:
    """Create a DNS-pinned Django SMTP backend from one organization configuration."""
    password = configuration.get_password()
    if not password:
        raise EmailConfigurationError(
            "Add an email password or app password before testing the connection."
        )

    if not configuration.smtp_host or not configuration.smtp_username:
        raise EmailConfigurationError(
            "SMTP host and username are required before testing the connection."
        )

    target = target or assert_public_smtp_target(
        configuration.smtp_host,
        configuration.smtp_port,
    )

    use_tls = (
        configuration.smtp_security == EmailConfiguration.Security.STARTTLS
    )
    use_ssl = configuration.smtp_security == EmailConfiguration.Security.SSL

    return PinnedEmailBackend(
        host=target.hostname,
        port=target.port,
        connect_ip=target.connect_ip,
        username=configuration.smtp_username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        timeout=15,
        fail_silently=False,
    )


def test_email_configuration(configuration: EmailConfiguration) -> None:
    """Authenticate to the configured SMTP server without sending a message."""
    target = assert_public_smtp_target(
        configuration.smtp_host,
        configuration.smtp_port,
    )
    backend = build_email_backend(configuration, target=target)

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
    attachments=None,
) -> int:
    """Send through the organization's connected mailbox without DNS rebinding."""
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

    from apps.core.fairness import admit_provider_start

    allowed, retry_after, _scope = admit_provider_start(
        provider="smtp",
        account_id=organization.pk,
        account_limit=settings.EMAIL_ORGANIZATION_SENDS_PER_MINUTE,
        global_limit=settings.EMAIL_GLOBAL_SENDS_PER_MINUTE,
    )
    if not allowed:
        raise EmailConfigurationError(
            "The connected email provider is temporarily busy. "
            f"Retry in about {retry_after} seconds."
        )

    target = assert_public_smtp_target(
        configuration.smtp_host,
        configuration.smtp_port,
    )
    backend = build_email_backend(configuration, target=target)
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
    for attachment in list(attachments or []):
        if not isinstance(attachment, (tuple, list)) or len(attachment) != 3:
            raise EmailConfigurationError("Email attachment metadata is invalid.")
        filename, content, mimetype = attachment
        message.attach(
            str(filename or "attachment"),
            content,
            str(mimetype or "application/octet-stream"),
        )

    try:
        return message.send(fail_silently=False)
    except smtplib.SMTPResponseException as exc:
        if 400 <= int(exc.smtp_code or 0) < 500:
            from apps.core.observability import increment

            increment("provider.throttled", labels={"provider": "smtp"})
        logger.exception(
            "Configured email delivery was rejected for organization %s.",
            organization.pk,
        )
        raise EmailConfigurationError(
            "The email provider temporarily rejected delivery."
            if 400 <= int(exc.smtp_code or 0) < 500
            else "The email provider rejected delivery."
        ) from exc
    except Exception as exc:
        logger.exception(
            "Configured email delivery failed for organization %s.",
            organization.pk,
        )
        raise EmailConfigurationError(
            "The email could not be sent through the connected mailbox."
        ) from exc
