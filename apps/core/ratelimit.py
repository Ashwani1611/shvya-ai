"""
Minimal IP(+key)-based rate limiting for plain Django views
(login forms, password reset, etc.) that don't go through DRF.

Uses the already-configured Redis cache backend, so no new
dependency (e.g. django-ratelimit) is required. If you'd rather
standardize on a dedicated library later, this can be swapped
out without changing call sites much.
"""

from functools import wraps

from django.core.cache import cache
from django.http import HttpResponse


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")

    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR", "unknown")


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