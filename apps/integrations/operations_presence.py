"""Shared visibility rules for active SHVYA Operations support presence."""

from datetime import timedelta

from django.utils import timezone

from apps.integrations.operations_models import OperationsSupportSession


SUPPORT_PRESENCE_WINDOW = timedelta(minutes=15)


def open_support_sessions(*, organization):
    """Return live, unclosed Superadmin support contexts for one tenant."""

    now = timezone.now()
    return (
        OperationsSupportSession.objects.filter(
            organization=organization,
            ended_at__isnull=True,
            token__revoked_at__isnull=True,
            token__refresh_expires_at__gt=now,
        )
        .select_related("actor", "token")
        .order_by("-last_seen_at", "-started_at")
    )


def support_session_recently_active(session, *, now=None):
    """Return whether one open context had Operations activity recently."""

    if session is None:
        return False
    now = now or timezone.now()
    return bool(
        session.ended_at is None
        and session.last_seen_at
        and session.last_seen_at >= now - SUPPORT_PRESENCE_WINDOW
    )


def visible_support_sessions(*, organization):
    """Return recently active live support contexts for one tenant."""

    return open_support_sessions(
        organization=organization,
    ).filter(
        last_seen_at__gte=timezone.now() - SUPPORT_PRESENCE_WINDOW,
    )
