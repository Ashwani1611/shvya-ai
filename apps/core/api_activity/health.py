"""Bounded organisation health and review discovery using the explorer's scope.

Read-only analysis. Discovery creates reviews, never customer/provider operations.
"""
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.core.api_activity.queries import (
    ISSUE_STATUSES, RANGE_BY_HOURS, RANGES, UNATTRIBUTED, attention_filter,
    diagnosis, explorer_url, filtered_activities, overdue_filter, parse_filters,
    serialize_activity,
)
from apps.core.models import APIActivity, APIIncident


def operations_health(*, hours=24, organization_id=None, filters=None, now=None):
    now = now or timezone.now()
    if filters is None:
        if hours not in RANGE_BY_HOURS:
            raise ValueError("Unsupported time window")
        params = {"range": RANGE_BY_HOURS[hours]}
    else:
        params = dict(filters)
    if organization_id is not None:
        params["organization"] = str(organization_id)
    scope = parse_filters(params)
    # Like the overview cards, health compares outcomes within the selected
    # time/tenant/integration/type/search scope, independent of the outcome tab.
    scope["status"] = ""
    base = filtered_activities(scope, now=now)
    attention = attention_filter(now)
    cohorts = list(base.values("organization_id", "organization__name").annotate(
        total=Count("pk"),
        completed=Count("pk", filter=Q(status="completed")),
        failures=Count("pk", filter=Q(status__in=ISSUE_STATUSES)),
        attention=Count("pk", filter=attention),
        overdue=Count("pk", filter=overdue_filter(now)),
        unknown=Count("pk", filter=Q(status="unknown")),
        active=Count("pk", filter=Q(status__in=("queued", "processing", "accepted"))),
        open_incidents=Count("incident", filter=Q(incident__status__in=("open", "acknowledged"))),
    ).order_by("-attention", "-total", "organization_id")[:100])
    for group in cohorts:
        group["organization_id"] = str(group["organization_id"]) if group["organization_id"] else None
        group["organization_name"] = group.pop("organization__name") or "Unattributed / platform"
        group["observed_success_percent"] = round(100 * group["completed"] / group["total"], 1) if group["total"] else None
        group["attention_url"] = explorer_url(
            scope, status="attention", organization=group["organization_id"] or UNATTRIBUTED,
        )
    groups = list(base.filter(attention).values(
        "organization_id", "organization__name", "integration", "error_category", "error_code",
    ).annotate(count=Count("pk")).order_by("-count")[:30])
    for group in groups:
        group["organization_id"] = str(group["organization_id"]) if group["organization_id"] else None
        group["organization_name"] = group.pop("organization__name") or "Unattributed / platform"
    recent = list(base.filter(attention).select_related("organization")
                  .defer("metadata").order_by("-created_at", "-pk")[:20])
    return {
        "window_hours": round(RANGES[scope["range"]][0] * 24),
        "generated_at": now.isoformat(), "filters": scope,
        "organizations": cohorts, "issue_cohorts": groups,
        "recent_attention": [
            {"activity": serialize_activity(row, now=now), "diagnostic_guidance": diagnosis(row)}
            for row in recent
        ],
        "limitations": [
            "Recorded API steps are not a complete count of business transactions.",
            "A completed step does not establish provider delivery or successful lead creation.",
            "Diagnostic guidance is rules-based and does not prove root cause.",
            "No automatic replay is performed; uncertain sends must be reconciled first.",
            "Only the top 100 organisation cohorts by attention count, then volume, are returned.",
            "Health compares all outcomes in the selected scope; the outcome filter is not applied.",
            "Attention includes overdue work; failures excludes overdue-only work for API compatibility.",
        ],
    }


def discover_attention(*, hours=None, limit=100):
    """Review retained attention evidence, oldest first, with bounded batch size.

    The scheduled/default scan has no creation-time cutoff, so a long-running
    operation or an old unreviewed backlog cannot age out of discovery. An
    explicit manual lookback restricts issue outcomes by updated_at, never by
    created_at; overdue open work is always eligible. Row locks and the unique
    activity link make repeated/concurrent scans safe without replaying work.
    """
    if (hours is not None and not 1 <= hours <= 720) or not 1 <= limit <= 500:
        raise ValueError("Invalid scan bounds")
    from django.db import transaction
    from apps.core.api_activity.operations import needs_attention

    now = timezone.now()
    candidates = APIActivity.objects.filter(attention_filter(now), incident__isnull=True)
    if hours is not None:
        candidates = candidates.filter(
            Q(updated_at__gte=now - timedelta(hours=hours)) | overdue_filter(now)
        )
    candidate_ids = list(candidates.order_by("created_at", "pk").values_list("pk", flat=True)[:limit])
    created = 0
    for pk in candidate_ids:
        with transaction.atomic():
            activity = APIActivity.objects.select_for_update().filter(pk=pk).first()
            if not activity or not needs_attention(activity):
                continue
            _, was_created = APIIncident.objects.get_or_create(
                activity=activity,
                defaults={"status": "open", "severity": "high" if activity.status in ("failed", "unknown") else "medium",
                          "due_at": timezone.now() + timedelta(hours=24)},
            )
            created += int(was_created)
    return {"opened": created, "scanned_max": limit, "window_hours": hours}
