from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


WEBHOOK_SECRET_HEADER = "X-SHVYA-WEBHOOK-SECRET"
WEBHOOK_DELIVERY_HEADER = "X-SHVYA-WEBHOOK-ID"
WEBHOOK_USER_AGENT = "SHVYA-Webhook/1.0"
MAX_WEBHOOK_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class ValidatedWebhookTarget:
    url: str
    hostname: str
    port: int
    connect_ip: str
    request_target: str
    host_header: str


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a validated IP while verifying the hostname."""

    def __init__(self, *, hostname, connect_ip, port, timeout):
        context = ssl.create_default_context()
        try:
            context.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:  # pragma: no cover
            pass
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._connect_ip = connect_ip

    def connect(self):
        raw_socket = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


def _validate_ip_address(ip_value):
    ip_obj = ipaddress.ip_address(ip_value)

    if not ip_obj.is_global:
        raise ValidationError(
            "Webhook URL must resolve to a public internet address."
        )


def validate_webhook_url(value):
    """Validate a stored webhook URL without requiring DNS to be reachable yet."""
    value = str(value or "").strip()

    if not value:
        raise ValidationError("Webhook URL is required.")

    URLValidator(schemes=["https"])(value)
    parsed = urlparse(value)

    if parsed.scheme.lower() != "https":
        raise ValidationError("Webhook URL must use HTTPS.")

    if parsed.username or parsed.password:
        raise ValidationError(
            "Webhook URL must not contain embedded username or password credentials."
        )

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ValidationError("Webhook URL must include a hostname.")

    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValidationError("Webhook URL cannot target localhost.")

    try:
        _validate_ip_address(hostname)
    except ValueError:
        pass

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Webhook URL contains an invalid port.") from exc

    if port not in {None, 443}:
        raise ValidationError("Webhook URL must use the standard HTTPS port.")

    return value


def _resolve_public_addresses(hostname, port):
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [literal]
    else:
        try:
            records = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValidationError(
                "Webhook hostname could not be resolved."
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
        raise ValidationError("Webhook hostname could not be resolved.")

    if any(not address.is_global for address in addresses):
        raise ValidationError(
            "Webhook URL must resolve only to public internet addresses."
        )

    return tuple(str(address) for address in addresses)


def assert_public_webhook_target(value):
    """Resolve once and bind delivery to the exact validated public IP."""
    value = validate_webhook_url(value)
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").strip().rstrip(".")
    port = parsed.port or 443
    addresses = _resolve_public_addresses(hostname, port)

    request_target = parsed.path or "/"
    if parsed.params:
        request_target += f";{parsed.params}"
    if parsed.query:
        request_target += f"?{parsed.query}"

    try:
        literal_host = ipaddress.ip_address(hostname)
    except ValueError:
        literal_host = None
    host_header = f"[{hostname}]" if literal_host and literal_host.version == 6 else hostname

    return ValidatedWebhookTarget(
        url=value,
        hostname=hostname,
        port=port,
        connect_ip=addresses[0],
        request_target=request_target,
        host_header=host_header,
    )


def post_webhook_json(target, *, payload, headers, timeout):
    """POST JSON without a second DNS lookup, preventing DNS rebinding."""
    if not isinstance(target, ValidatedWebhookTarget):
        target = assert_public_webhook_target(target)

    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request_headers = {
        **headers,
        "Host": target.host_header,
        "Content-Length": str(len(body)),
        "Connection": "close",
        "Accept-Encoding": "identity",
    }

    connection = _PinnedHTTPSConnection(
        hostname=target.hostname,
        connect_ip=target.connect_ip,
        port=target.port,
        timeout=timeout,
    )
    response = None
    try:
        connection.request(
            "POST",
            target.request_target,
            body=body,
            headers=request_headers,
        )
        response = connection.getresponse()
        raw = response.read(MAX_WEBHOOK_RESPONSE_BYTES + 1)
        if len(raw) > MAX_WEBHOOK_RESPONSE_BYTES:
            raw = raw[:MAX_WEBHOOK_RESPONSE_BYTES]
        return response.status, raw.decode("utf-8", errors="replace")
    finally:
        if response is not None:
            response.close()
        connection.close()


def build_lead_webhook_payload(lead, event_type):
    return {
        "lead_id": str(lead.id),
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "notes": lead.notes,
        "stage": lead.stage.name if lead.stage_id else "",
        "pipeline": lead.pipeline.name if lead.pipeline_id else "",
        "event_type": event_type,
        "custom_attributes": lead.attributes or {},
    }
