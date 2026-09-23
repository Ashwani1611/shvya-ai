"""Fail-soft per-tenant concurrency protection for expensive HTTP bursts."""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

from apps.core.observability import increment


class TenantConcurrencyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "crm_user", None)
        organization_id = getattr(user, "organization_id", None)
        limit = int(getattr(settings, "TENANT_HTTP_CONCURRENCY_LIMIT", 0) or 0)
        if not organization_id or limit <= 0:
            return self.get_response(request)

        digest = hashlib.sha256(str(organization_id).encode("utf-8")).hexdigest()[:24]
        key = f"shvya:http-concurrency:{digest}"
        acquired = False
        try:
            if cache.add(key, 1, timeout=120):
                active = 1
            else:
                active = cache.incr(key)
            if int(active) > limit:
                try:
                    cache.decr(key)
                except (ValueError, TypeError):
                    pass
                increment("http.tenant_concurrency_rejected")
                response = JsonResponse(
                    {"detail": "This workspace is temporarily busy. Try again shortly."},
                    status=429,
                )
                response["Retry-After"] = "1"
                return response
            acquired = True
        except Exception:
            # Admission controls must not make Redis a new single point of
            # failure; normal application paths have their own DB/provider limits.
            return self.get_response(request)

        try:
            return self.get_response(request)
        finally:
            if acquired:
                try:
                    cache.decr(key)
                except (ValueError, TypeError):
                    pass
