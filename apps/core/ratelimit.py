"""
Minimal IP(+key)-based rate limiting for plain Django views
(login forms, password reset, etc.) that don't go through DRF.

Uses the already-configured Redis cache backend, so no new
dependency (e.g. django-ratelimit) is required. If you'd rather
standardize on a dedicated library later, this can be swapped
out without changing call sites much.
"""

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


def ratelimit(key_func=None, limit=5, window=300):
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
        )
        def crm_forgot_password_view(request):
            ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):

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
