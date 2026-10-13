"""Bounded organisation health, error cohorts and safe diagnostic hints.

Read-only analysis. No provider calls, customer content, or speculative AI output.
"""
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from django.urls import reverse

from apps.core.api_activity.queries import ISSUE_STATUSES, diagnosis, serialize_activity
from apps.core.models import APIActivity, APIIncident


def operations_health(*, hours=24, organization_id=None):
    if hours not in (1, 24, 168, 720):
        raise ValueError("Unsupported time window")
    since = timezone.now() - timedelta(hours=hours)
    base = APIActivity.objects.filter(created_at__gte=since)
    if organization_id:
        base = base.filter(organization_id=organization_id)
    # Aggregate in SQL. Unattributed traffic remains a distinct entry.
    cohorts = list(base.values("organization_id", "organization__name").annotate(
        total=Count("pk"),
        completed=Count("pk", filter=Q(status="completed")),
        failures=Count("pk", filter=Q(status__in=ISSUE_STATUSES)),
        unknown=Count("pk", filter=Q(status="unknown")),
        active=Count("pk", filter=Q(status__in=("queued", "processing", "accepted"))),
        open_incidents=Count("incident", filter=Q(incident__status__in=("open", "acknowledged"))),
    ).order_by("-failures", "-total")[:100])
    for group in cohorts:
        group["organization_id"] = str(group["organization_id"]) if group["organization_id"] else None
        group["organization_name"] = group.pop("organization__name") or "Unattributed / platform"
        group["observed_success_percent"] = round(100 * group["completed"] / group["total"], 1) if group["total"] else None
        group["attention_url"] = reverse("superadmin-api") + (
            "?status=attention&organization=" + group["organization_id"]
            if group["organization_id"] else "?status=attention"
        )
        # Observed completion is not end-to-end provider delivery or an SLA.
    groups = list(base.filter(status__in=ISSUE_STATUSES).values(
        "organization_id", "organization__name", "integration", "error_category", "error_code",
    ).annotate(count=Count("pk")).order_by("-count")[:30])
    for group in groups:
        group["organization_id"] = str(group["organization_id"]) if group["organization_id"] else None
        group["organization_name"] = group.pop("organization__name") or "Unattributed / platform"
    recent = list(base.filter(status__in=ISSUE_STATUSES).select_related("organization")
                  .defer("metadata").order_by("-created_at")[:20])
    return {
        "window_hours": hours, "generated_at": timezone.now().isoformat(),
        "organizations": cohorts, "issue_cohorts": groups,
        "recent_attention": [
            {"activity": serialize_activity(row), "diagnostic_guidance": diagnosis(row)}
            for row in recent
        ],
        "limitations": [
            "Recorded API steps are not a complete count of business transactions.",
            "A completed step does not establish provider delivery or successful lead creation.",
            "Diagnostic guidance is rules-based and does not prove root cause.",
            "No automatic replay is performed; uncertain sends must be reconciled first.",
            "Only the 100 highest-volume organisation cohorts are returned.",
        ],
    }


def discover_attention(*, hours=24, limit=100):
    """Create human-review tickets from recorded failures; never retry actions.

    Requires singleton scheduling / manual invocation; transactional per-activity
    get_or_create prevents duplicates in concurrent workers.
    """
    if not 1 <= hours <= 720 or not 1 <= limit <= 500:
        raise ValueError("Invalid scan bounds")
    from django.db import transaction
    from apps.core.api_activity.operations import needs_attention

    since = timezone.now() - timedelta(hours=hours)
    now = timezone.now()
    candidates = APIActivity.objects.filter(
        Q(status__in=ISSUE_STATUSES)
        | Q(status__in=("queued", "processing", "accepted"), expected_by__lt=now),
        created_at__gte=since, incident__isnull=True,
    ).order_by("created_at").values_list("pk", flat=True)[:limit]
    created = 0
    for pk in candidates:
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
