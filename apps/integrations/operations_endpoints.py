"""Canonical public endpoint identifiers for SHVYA Operations MCP/OAuth."""

from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


PRODUCTION_OPERATIONS_ORIGIN = "https://dashboard.shvya-ai.com"


def operations_public_origin() -> str:
    origin = str(
        getattr(
            settings,
            "OPERATIONS_PUBLIC_ORIGIN",
            PRODUCTION_OPERATIONS_ORIGIN,
        )
        or ""
    ).strip().rstrip("/")
    parsed = urlparse(origin)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ImproperlyConfigured(
            "OPERATIONS_PUBLIC_ORIGIN contains an invalid port."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ImproperlyConfigured(
            "OPERATIONS_PUBLIC_ORIGIN must be an absolute HTTP(S) origin."
        )
    if port is not None and not 1 <= port <= 65535:
        raise ImproperlyConfigured(
            "OPERATIONS_PUBLIC_ORIGIN contains an invalid port."
        )
    if parsed.scheme != "https" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "testserver",
    }:
        raise ImproperlyConfigured(
            "OPERATIONS_PUBLIC_ORIGIN must use HTTPS outside local tests."
        )
    return origin


def operations_public_url(path: str) -> str:
    normalized_path = "/" + str(path or "").lstrip("/")
    return operations_public_origin() + normalized_path


def operations_issuer() -> str:
    return operations_public_url("/operations")


def operations_resource() -> str:
    return operations_public_url("/operations/mcp/")


def operations_resource_metadata_url() -> str:
    return operations_public_url(
        "/.well-known/oauth-protected-resource/operations/mcp/"
    )


def operations_authorization_url() -> str:
    return operations_public_url("/operations/oauth/authorize")


def operations_token_url() -> str:
    return operations_public_url("/operations/oauth/token")


def operations_registration_url() -> str:
    return operations_public_url("/operations/oauth/register")


def operations_revocation_url() -> str:
    return operations_public_url("/operations/oauth/revoke")
