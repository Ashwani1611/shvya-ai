"""Read-only, tenant-scoped organisation dashboard aggregates."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Count, Q
from django.db.models.functions import Trunc
from django.utils import timezone

from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead
from apps.followups.models import FollowupExecution, LeadSequenceState


def dashboard_window(params, timezone_name):
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    today = timezone.localdate(timezone=tz)
    notice = ""
    try:
        start = date.fromisoformat(params.get("from", "") or (today - timedelta(days=28)).isoformat())
        end = date.fromisoformat(params.get("to", "") or today.isoformat())
        if start > end or end == date.max or (end - start).days > 366:
            raise ValueError
    except (ValueError, TypeError):
        start, end = today - timedelta(days=28), today
        notice = "Choose a valid date range of up to 367 days. Showing the last 29 days."
    granularity = params.get("granularity", "day")
    if granularity not in ("hour", "day", "month"):
        granularity = "day"
    if granularity == "hour" and (end - start).days > 6:
        granularity = "day"
        notice = "Hourly charts support up to 7 days. Showing daily totals for this range."
    return {
        "from": start.isoformat(), "to": end.isoformat(), "granularity": granularity,
        "start": datetime.combine(start, time.min, tzinfo=tz),
        "end": datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz),
        "tz": tz, "notice": notice,
    }


def organization_analytics(organization, params):
    window = dashboard_window(params, organization.timezone)
    leads = Lead.objects.filter(organization=organization)
    selected = leads.filter(created_at__gte=window["start"], created_at__lt=window["end"])
    messages = WhatsAppMessage.objects.filter(
        organization=organization, created_at__gte=window["start"], created_at__lt=window["end"],
    )
    executions = FollowupExecution.objects.filter(
        organization=organization, lead__organization=organization, lead__is_operations_test=False,
        finished_at__gte=window["start"], finished_at__lt=window["end"], status="sent",
    )
    def series(queryset, field):
        rows = queryset.order_by().annotate(bucket=Trunc(field, window["granularity"], tzinfo=window["tz"])).values("bucket").annotate(total=Count("pk")).order_by("bucket")
        values = {row["bucket"]: row["total"] for row in rows}
        cursor = window["start"]
        if window["granularity"] == "month":
            cursor = cursor.replace(day=1)
        result = []
        while cursor < window["end"]:
            result.append({"label": cursor.strftime("%d %b %H:%M" if window["granularity"] == "hour" else "%d %b %Y"), "value": values.get(cursor, 0)})
            if window["granularity"] == "month":
                cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            else:
                cursor += timedelta(hours=1) if window["granularity"] == "hour" else timedelta(days=1)
        return result

    breakdowns = []
    source_names = dict(Lead._meta.get_field("lead_source").choices)
    for title, field in (("Leads by source", "lead_source"), ("Leads by stage", "stage__name"), ("Leads by pipeline", "pipeline__name")):
        rows = list(selected.order_by().values(field).annotate(total=Count("pk")).order_by("-total", field))
        breakdowns.append({"title": title, "rows": [{"label": source_names.get(row[field], row[field]) if field == "lead_source" else row[field], "value": row["total"]} for row in rows]})
    counts = messages.aggregate(
        inbound=Count("pk", filter=Q(direction="inbound")),
        outbound=Count("pk", filter=Q(direction="outbound")),
        delivered=Count("pk", filter=Q(direction="outbound", status__in=["delivered", "read"])),
        failed=Count("pk", filter=Q(direction="outbound", status="failed")),
        queued=Count("pk", filter=Q(direction="outbound", status="queued")),
    )
    pending = LeadSequenceState.objects.filter(
        organization=organization, lead__organization=organization, lead__is_operations_test=False,
        status="active", lead_auto_followup_enabled=True, upcoming_send_at__isnull=False,
    )
    now = timezone.now()
    info = getattr(organization, "org_info", None)
    checks = [{"label": label, "ready": bool(getattr(info, field, ""))} for label, field in (
        ("Business description", "about"), ("AI Playbook", "ai_playbook"), ("Bot languages", "bot_languages"),
    )]
    latest = leads.order_by("-created_at").values_list("created_at", flat=True).first()
    return {
        "window": window, "total_leads": leads.count(), "selected_leads": selected.count(),
        "messages": counts, "breakdowns": breakdowns, "checks": checks,
        "last_lead_at": latest, "pending_count": pending.count(),
        "overdue_count": pending.filter(upcoming_send_at__lt=now).count(),
        "pending": list(pending.select_related("lead", "sequence", "next_step").order_by("upcoming_send_at")[:20]),
        "charts": [
            {"title": "Leads over time", "rows": series(selected, "created_at")},
            {"title": "WhatsApp outbound messages created", "rows": series(messages.filter(direction="outbound"), "created_at")},
            {"title": "Cadence emails sent", "rows": series(executions.filter(step__step_type="email"), "finished_at")},
            {"title": "Cadence WhatsApp messages sent", "rows": series(executions.filter(step__step_type="whatsapp"), "finished_at")},
        ],
    }
