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

from apps.integrations.services.public_network import (
    PublicNetworkTargetError,
    resolve_public_target,
)


WEBHOOK_SECRET_HEADER = "X-SHVYA-WEBHOOK-SECRET"
WEBHOOK_DELIVERY_HEADER = "X-SHVYA-WEBHOOK-ID"
WEBHOOK_USER_AGENT = "SHVYA-Webhook/1.0"
MAX_WEBHOOK_RESPONSE_BYTES = 64 * 1024


class WebhookRequestError(RuntimeError):
    """Raised when a pinned webhook request cannot be completed safely."""


@dataclass(frozen=True)
class WebhookTarget:
    url: str
    hostname: str
    port: int
    connect_ip: str
    request_target: str
    host_header: str


@dataclass(frozen=True)
class WebhookHTTPResponse:
    status_code: int
    text: str


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a validated IP with hostname TLS verification."""

    def __init__(self, *, hostname: str, connect_ip: str, port: int, timeout: float):
        context = ssl.create_default_context()
        try:
            context.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:  # pragma: no cover - platform dependent
            pass
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=context,
        )
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


def _validate_literal_public_ip(hostname: str) -> None:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValidationError(
            "Webhook URL must target a public internet address."
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

    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not hostname:
        raise ValidationError("Webhook URL must include a hostname.")

    if hostname.casefold() == "localhost" or hostname.casefold().endswith(".localhost"):
        raise ValidationError("Webhook URL cannot target localhost.")

    _validate_literal_public_ip(hostname)

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Webhook URL contains an invalid port.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValidationError("Webhook URL contains an invalid port.")

    return value


def assert_public_webhook_target(value):
    """Resolve once and bind delivery to the exact validated public IP."""
    value = validate_webhook_url(value)
    parsed = urlparse(value)
    hostname = str(parsed.hostname or "").strip().rstrip(".")
    port = parsed.port or 443

    try:
        resolved = resolve_public_target(hostname, port)
    except PublicNetworkTargetError as exc:
        raise ValidationError(str(exc)) from exc

    request_target = parsed.path or "/"
    if parsed.params:
        request_target += f";{parsed.params}"
    if parsed.query:
        request_target += f"?{parsed.query}"

    try:
        literal = ipaddress.ip_address(resolved.hostname)
    except ValueError:
        literal = None

    host_header = (
        f"[{resolved.hostname}]"
        if literal is not None and literal.version == 6
        else resolved.hostname
    )
    if parsed.port is not None:
        host_header = f"{host_header}:{port}"

    return WebhookTarget(
        url=value,
        hostname=resolved.hostname,
        port=resolved.port,
        connect_ip=resolved.connect_ip,
        request_target=request_target,
        host_header=host_header,
    )


def _read_response_text(response: http.client.HTTPResponse) -> str:
    declared = str(response.getheader("Content-Length", "") or "").strip()
    if declared:
        try:
            declared_size = int(declared)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > MAX_WEBHOOK_RESPONSE_BYTES:
            raise WebhookRequestError("Webhook response exceeded the safe size limit.")

    raw = response.read(MAX_WEBHOOK_RESPONSE_BYTES + 1)
    if len(raw) > MAX_WEBHOOK_RESPONSE_BYTES:
        raise WebhookRequestError("Webhook response exceeded the safe size limit.")
    return raw.decode("utf-8", errors="replace")


def send_webhook_request(
    target: WebhookTarget,
    *,
    payload,
    headers,
    timeout: float,
) -> WebhookHTTPResponse:
    """POST JSON to the already-resolved public IP without another DNS lookup."""
    body = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    request_headers = {
        **dict(headers or {}),
        "Host": target.host_header,
        "Accept-Encoding": "identity",
        "Connection": "close",
        "Content-Length": str(len(body)),
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
        return WebhookHTTPResponse(
            status_code=response.status,
            text=_read_response_text(response),
        )
    except WebhookRequestError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise WebhookRequestError(
            f"Webhook connection failed: {type(exc).__name__}."
        ) from exc
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
