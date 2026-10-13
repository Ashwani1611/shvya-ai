"""Operator-facing symptom cohorts, scoped by the current incident filters.

Grouping is observational only: matching codes do not prove a shared root cause.
"""
from django.db.models import Count, Max, Q
from django.utils import timezone


def incident_cohorts(scope, *, limit=15):
    now = timezone.now()
    overdue = Q(due_at__lt=now) & Q(status__in=("open", "acknowledged")) & (
        Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now)
    )
    return list(scope.filter(status__in=("open", "acknowledged")).values(
        "activity__organization_id", "activity__organization__name",
        "activity__integration", "activity__error_code", "activity__error_category",
    ).annotate(
        affected_activities=Count("pk"),
        distinct_recorded_entity_ids=Count("activity__entity_id", distinct=True, filter=~Q(activity__entity_id="")),
        activities_without_entity_id=Count("pk", filter=Q(activity__entity_id="")),
        overdue_reviews=Count("pk", filter=overdue),
        high_severity=Count("pk", filter=Q(severity="high")),
        last_recorded=Max("activity__created_at"),
    ).order_by("-overdue_reviews", "-high_severity", "-affected_activities")[:limit])


def attention_queue(scope, *, limit=25):
    """Priority ordering of existing reviews; no notification delivery or replay."""
    return list(scope.filter(status__in=("open", "acknowledged")).select_related(
        "activity__organization", "owner",
    ).order_by("due_at", "-created_at")[:limit])
