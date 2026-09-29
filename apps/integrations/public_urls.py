"""Canonical public URLs for externally discovered SHVYA integrations."""

from django.conf import settings


def operations_public_origin(request):
    """Return the trusted public origin for Operations MCP/OAuth URLs.

    Production and staging pin this setting explicitly so OAuth discovery cannot
    be poisoned by a stale or incorrect reverse-proxy Host header. Local/test
    environments keep request-derived behavior unless they opt in.
    """

    configured = str(
        getattr(settings, "OPERATIONS_PUBLIC_BASE_URL", "") or ""
    ).strip().rstrip("/")
    if configured:
        return configured
    return request.build_absolute_uri("/").rstrip("/")


def operations_public_url(request, path):
    """Build an Operations public URL from the trusted environment origin."""

    path = str(path or "")
    if not path.startswith("/"):
        path = "/" + path
    return operations_public_origin(request) + path
