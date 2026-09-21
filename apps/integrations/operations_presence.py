"""Shared visibility rules for active SHVYA Operations support presence."""

from datetime import timedelta

from django.utils import timezone

from apps.integrations.operations_models import OperationsSupportSession


SUPPORT_PRESENCE_WINDOW = timedelta(minutes=15)


def visible_support_sessions(*, organization):
    """Return recently active, live Superadmin support sessions for one tenant."""

    now = timezone.now()
    return (
        OperationsSupportSession.objects.filter(
            organization=organization,
            ended_at__isnull=True,
            token__revoked_at__isnull=True,
            token__expires_at__gt=now,
            last_seen_at__gte=now - SUPPORT_PRESENCE_WINDOW,
        )
        .select_related("actor")
        .order_by("-last_seen_at", "-started_at")
    )
