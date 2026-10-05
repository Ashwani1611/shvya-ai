"""Shared call metrics. Rates use call counts; talk averages use answered calls."""

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from datetime import timedelta


def format_duration(value):
    if value is None:
        return "—"
    try:
        seconds = max(0, int(float(value) + 0.5))
    except (ValueError, TypeError, OverflowError):
        return "—"
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hr")
    if minutes:
        parts.append(f"{minutes} min")
    if seconds or not parts:
        parts.append(f"{seconds} sec")
    return " ".join(parts)


def metric_expressions():
    answered = Q(status="answered")
    now = timezone.now()
    return dict(
        total=Count("id"),
        answered=Count("id", filter=answered),
        incoming=Count("id", filter=Q(direction="incoming")),
        outgoing=Count("id", filter=Q(direction="outgoing")),
        missed=Count("id", filter=Q(status="missed")),
        average_talk=Avg("talk_duration_seconds", filter=answered),
        total_talk=Sum("talk_duration_seconds", filter=answered),
        total_ring=Sum("ring_duration_seconds"),
        measured_ring=Count("id", filter=Q(ring_duration_seconds__gt=0)),
        average_ring=Avg(
            "ring_duration_seconds", filter=Q(ring_duration_seconds__gt=0)
        ),
        average_response=Avg(
            "ring_duration_seconds",
            filter=answered & Q(direction="incoming", ring_duration_seconds__gt=0),
        ),
        converted=Count("id", filter=Q(disposition="converted")),
        dispositioned=Count("id", filter=~Q(disposition="")),
        high_intent=Count("id", filter=Q(intelligence__intent="high")),
        analyzed=Count("intelligence"),
        average_ai_score=Avg("intelligence__ai_score"),
        linked=Count("id", filter=Q(lead__isnull=False)),
        overdue=Count("id", filter=Q(follow_up_required=True, follow_up_at__lt=now)),
        followups=Count(
            "id",
            filter=Q(
                follow_up_required=True,
                follow_up_at__gte=now,
                follow_up_at__lte=now + timedelta(days=1),
            ),
        ),
    )


def normalize_metrics(row):
    for key in ("average_talk", "total_talk", "total_ring", "average_ring"):
        row[key] = round(row[key] or 0)
    total = row["total"]
    row["answer_rate"] = round(100 * row["answered"] / total, 1) if total else 0
    row["conversion_rate"] = (
        round(100 * row["converted"] / row["answered"], 1) if row["answered"] else 0
    )
    row["disposition_rate"] = (
        round(100 * row["dispositioned"] / total, 1) if total else 0
    )
    row["unclassified"] = total - row["dispositioned"]
    return row


def call_metrics(qs):
    return normalize_metrics(qs.aggregate(**metric_expressions()))


def call_outcome_groups(qs, dispositions):
    """Stage-style counts include all matching calls, independently of pagination."""
    counts = dict(qs.values("disposition").annotate(total=Count("id")).values_list("disposition", "total"))
    groups = [
        {"code": "", "name": "All calls", "category": "all", "total": sum(counts.values())},
        {"code": "unclassified", "name": "Not classified", "category": "unclassified", "total": counts.get("", 0)},
    ]
    known = set()
    for disposition in dispositions:
        known.add(disposition.code)
        if disposition.is_active or counts.get(disposition.code):
            groups.append({
                "code": disposition.code, "name": disposition.name,
                "category": disposition.category, "total": counts.get(disposition.code, 0),
            })
    for code, total in counts.items():
        if code and code not in known:
            groups.append({"code": code, "name": code.replace("_", " ").title(), "category": "unclassified", "total": total})
    return groups


def team_metrics(qs):
    rows = (
        qs.exclude(user_id__isnull=True)
        .values("user_id", "user__name", "user__email")
        .annotate(**metric_expressions())
        .order_by("-total", "user__name")
    )
    result = []
    for row in rows:
        normalize_metrics(row)
        row["calls"] = row["total"]
        result.append(row)
    return result
