from django.conf import settings
from django.db import connection

from apps.accounts.models import User
from apps.crm.models import Lead
from apps.organizations.models import APIKey, Organization


def _check_database():
    """
    Check whether the configured database connection is available.
    """

    try:
        connection.ensure_connection()

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        return {
            "status": "healthy",
            "label": "Connected",
        }

    except Exception:
        return {
            "status": "error",
            "label": "Unavailable",
        }


def _check_django():
    """
    Check whether the Django application layer is available.
    """

    try:
        from django import get_version

        version = get_version()

        if version:
            return {
                "status": "healthy",
                "label": f"Django {version}",
            }

        return {
            "status": "warning",
            "label": "Unknown version",
        }

    except Exception:
        return {
            "status": "error",
            "label": "Unavailable",
        }


def _check_authentication():
    """
    Check whether the configured custom user model is available.
    """

    try:
        User.objects.exists()

        return {
            "status": "healthy",
            "label": "Healthy",
        }

    except Exception:
        return {
            "status": "error",
            "label": "Unavailable",
        }


def _check_crm():
    """
    Check whether the CRM database layer is available.
    """

    try:
        Lead.objects.exists()

        return {
            "status": "healthy",
            "label": "Healthy",
        }

    except Exception:
        return {
            "status": "error",
            "label": "Unavailable",
        }


def _check_api():
    """
    Check whether Django REST Framework and JWT configuration
    are available.
    """

    try:
        rest_framework_configured = bool(
            getattr(settings, "REST_FRAMEWORK", None)
        )

        jwt_configured = bool(
            getattr(settings, "SIMPLE_JWT", None)
        )

        if rest_framework_configured and jwt_configured:
            return {
                "status": "healthy",
                "label": "Configured",
            }

        return {
            "status": "warning",
            "label": "Incomplete",
        }

    except Exception:
        return {
            "status": "error",
            "label": "Unavailable",
        }


def shvya_admin_stats(request):
    """
    Provide SHVYA Admin dashboard statistics and
    real-time system status information.

    These values are calculated only for Django Admin
    requests.
    """

    if not request.path.startswith("/admin/"):
        return {}

    database_status = _check_database()
    django_status = _check_django()
    authentication_status = _check_authentication()
    crm_status = _check_crm()
    api_status = _check_api()

    all_statuses = [
        database_status,
        django_status,
        authentication_status,
        crm_status,
        api_status,
    ]

    if any(
        item["status"] == "error"
        for item in all_statuses
    ):
        overall_status = {
            "status": "error",
            "label": "Attention required",
        }

    elif any(
        item["status"] == "warning"
        for item in all_statuses
    ):
        overall_status = {
            "status": "warning",
            "label": "Check configuration",
        }

    else:
        overall_status = {
            "status": "healthy",
            "label": "System operational",
        }

    return {
        "shvya_admin_stats": {
            # ---------------------------------------------------------
            # Database statistics
            # ---------------------------------------------------------

            "organizations": Organization.objects.count(),
            "users": User.objects.count(),
            "leads": Lead.objects.count(),
            "api_keys": APIKey.objects.count(),

            # ---------------------------------------------------------
            # System status
            # ---------------------------------------------------------

            "system": {
                "overall": overall_status,

                "database": database_status,

                "django": django_status,

                "authentication": authentication_status,

                "crm": crm_status,

                "api": api_status,
            },
        }
    }