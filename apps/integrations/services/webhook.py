from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


WEBHOOK_SECRET_HEADER = "X-SHVYA-WEBHOOK-SECRET"
WEBHOOK_DELIVERY_HEADER = "X-SHVYA-WEBHOOK-ID"
WEBHOOK_USER_AGENT = "SHVYA-Webhook/1.0"


def _validate_ip_address(ip_value):
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

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValidationError("Webhook URL must include a hostname.")

    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValidationError("Webhook URL cannot target localhost.")

    try:
        _validate_ip_address(hostname)
    except ValueError:
        pass

    return value


def assert_public_webhook_target(value):
    """Resolve the target immediately before delivery to reduce SSRF risk."""
    value = validate_webhook_url(value)
    parsed = urlparse(value)
    hostname = parsed.hostname
    port = parsed.port or 443

    try:
        addresses = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValidationError(
            "Webhook hostname could not be resolved."
        ) from exc

    if not addresses:
        raise ValidationError("Webhook hostname could not be resolved.")

    for address in addresses:
        _validate_ip_address(address[4][0])

    return value


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
