import logging

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
def live(request):
    """Process-level liveness probe. Does not depend on external services."""
    return JsonResponse(
        {
            "status": "ok",
            "environment": getattr(settings, "APP_ENV", "production"),
        }
    )


@require_GET
def ready(request):
    """Readiness probe for dependencies required to serve normal traffic."""
    checks = {"database": False, "redis": False}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            checks["database"] = cursor.fetchone() == (1,)
    except Exception:
        logger.exception("Readiness database check failed")

    try:
        key = "shvya:health:ready"
        cache.set(key, "ok", timeout=10)
        checks["redis"] = cache.get(key) == "ok"
    except Exception:
        logger.exception("Readiness Redis check failed")

    healthy = all(checks.values())
    return JsonResponse(
        {
            "status": "ok" if healthy else "degraded",
            "environment": getattr(settings, "APP_ENV", "production"),
            "checks": checks,
        },
        status=200 if healthy else 503,
    )
