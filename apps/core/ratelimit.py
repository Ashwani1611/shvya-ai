"""
Minimal IP(+key)-based rate limiting for plain Django views
(login forms, password reset, etc.) that don't go through DRF.

Uses the already-configured Redis cache backend, so no new
dependency (e.g. django-ratelimit) is required. If you'd rather
standardize on a dedicated library later, this can be swapped
out without changing call sites much.
"""

import hashlib
import ipaddress
from functools import wraps

from django.core.cache import cache
from django.http import HttpResponse


_TRUSTED_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
    )
)


def _is_trusted_proxy_address(value):
    """
    Return True only for the loopback/private networks used by our reverse
    proxy and Docker networking.

    Do not rely on ``ipaddress.is_private`` here: Python intentionally treats
    some reserved/non-global ranges as private too, which would broaden the
    set of peers allowed to supply forwarding headers.
    """
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return False

    return any(address in network for network in _TRUSTED_PROXY_NETWORKS)


def _client_ip(request):
    remote_addr = str(request.META.get("REMOTE_ADDR", "unknown") or "unknown").strip()

    if _is_trusted_proxy_address(remote_addr):
        # Nginx is configured to overwrite these headers with $remote_addr,
        # rather than append client-supplied values, before proxying to Django.
        real_ip = request.META.get("HTTP_X_REAL_IP", "").strip()
        if real_ip:
            return real_ip

        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
            if candidate:
                return candidate

    return remote_addr


def _failure_cache_key(scope, request, *, identifier="", include_ip=True):
    """Build a privacy-preserving cache key for authentication failures."""
    parts = [str(scope or "auth").strip().casefold() or "auth"]

    if include_ip:
        parts.append(_client_ip(request))

    normalized_identifier = str(identifier or "").strip().casefold()
    if normalized_identifier:
        parts.append(normalized_identifier)

    if len(parts) == 1:
        raise ValueError("A failure-rate-limit bucket needs an IP or identifier.")

    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"authfail:{parts[0]}:{digest}"


def authentication_failure_is_limited(
    scope,
    request,
    *,
    identifier="",
    limit=10,
    include_ip=True,
):
    """Return True once the selected authentication-failure bucket is full."""
    key = _failure_cache_key(
        scope,
        request,
        identifier=identifier,
        include_ip=include_ip,
    )
    return int(cache.get(key, 0) or 0) >= int(limit)


def record_authentication_failure(
    scope,
    request,
    *,
    identifier="",
    window=300,
    include_ip=True,
):
    """Atomically count one failed authentication attempt in the cache."""
    key = _failure_cache_key(
        scope,
        request,
        identifier=identifier,
        include_ip=include_ip,
    )
    if cache.add(key, 1, timeout=window):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window)
        return 1


def clear_authentication_failures(
    scope,
    request,
    *,
    identifier="",
    include_ip=True,
):
    """Clear one failure bucket after a successful authentication."""
    key = _failure_cache_key(
        scope,
        request,
        identifier=identifier,
        include_ip=include_ip,
    )
    cache.delete(key)



def ratelimit(key_func=None, limit=5, window=300, methods=None):
    """
    Rate limit a view to `limit` requests per `window` seconds,
    keyed by client IP (and optionally an extra key, e.g. the
    submitted email/username, so one IP can't lock out everyone
    but also can't hammer one specific account).

    Returns HTTP 429 once the limit is exceeded within the window.

    Example:

        @ratelimit(limit=5, window=300)
        def superadmin_login_view(request):
            ...

        @ratelimit(
            key_func=lambda r: r.POST.get("email", ""),
            limit=5,
            window=900,
            methods=("POST",),
        )
        def crm_forgot_password_view(request):
            ...
    """

    limited_methods = {method.upper() for method in (methods or ())}

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if limited_methods and request.method.upper() not in limited_methods:
                return view_func(request, *args, **kwargs)

            ip = _client_ip(request)
            extra = key_func(request) if key_func else ""
            cache_key = f"ratelimit:{view_func.__name__}:{ip}:{extra}"

            try:
                count = cache.incr(cache_key)
            except ValueError:
                # Key doesn't exist yet -- first attempt in this window.
                cache.set(cache_key, 1, timeout=window)
                count = 1

            if count > limit:
                return HttpResponse(
                    "Too many attempts. Please try again later.",
                    status=429,
                )

            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
