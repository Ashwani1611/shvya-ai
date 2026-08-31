"""
Analytics aggregation queries. Every function here is read-only and
tenant-scoped by organization. Business logic for computing dashboard
numbers lives here rather than in views/templates (CLAUDE.md rule 2).

Widgets intentionally NOT implemented here (would require faking data):
  - AI Qualified Leads     -- needs an AI-qualification flag (Co-Pilot)
  - Auto Follow-Ups Sent   -- needs an automation send-log (Auto Follow-ups)
  - No Auto Follow-Ups     -- needs a per-lead automation-enabled flag
"""
from datetime import timedelta

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.analytics.models import AnalyticsSettings
from apps.crm.models.call import LeadCall
from apps.crm.models.lead import Lead
from apps.crm.models.reminder import LeadReminder


def _scope_leads(*, organization, pipeline_ids=None):
    qs = Lead.objects.filter(organization=organization)
    if pipeline_ids:
        qs = qs.filter(pipeline_id__in=pipeline_ids)
    return qs


def get_or_create_settings(*, organization):
    settings_obj, _ = AnalyticsSettings.objects.get_or_create(
        organization=organization,
    )
    return settings_obj


def save_settings(*, organization, hot_lead_stage, lead_won_stage, lead_lost_stage, stall_day_threshold):
    settings_obj = get_or_create_settings(organization=organization)
    settings_obj.hot_lead_stage = hot_lead_stage
    settings_obj.lead_won_stage = lead_won_stage
    settings_obj.lead_lost_stage = lead_lost_stage
    settings_obj.stall_day_threshold = stall_day_threshold
    settings_obj.save()
    return settings_obj


def get_overview_metrics(*, organization, pipeline_ids=None):
    leads = _scope_leads(organization=organization, pipeline_ids=pipeline_ids)
    total_leads = leads.count()

    calls = LeadCall.objects.filter(lead__organization=organization)
    if pipeline_ids:
        calls = calls.filter(lead__pipeline_id__in=pipeline_ids)

    call_totals = calls.aggregate(
        total_seconds=Sum("duration_seconds"),
        calls_done=Count("id"),
    )
    total_seconds = call_totals["total_seconds"] or 0

    return {
        "total_leads": total_leads,
        "total_call_minutes": total_seconds // 60,
        "calls_done": call_totals["calls_done"] or 0,
    }


def get_pending_reminder_count(*, organization, pipeline_ids=None):
    reminders = LeadReminder.objects.filter(
        lead__organization=organization,
        status="pending",
    )
    if pipeline_ids:
        reminders = reminders.filter(lead__pipeline_id__in=pipeline_ids)
    return reminders.values("lead_id").distinct().count()


def get_pending_replies_count(*, organization, pipeline_ids=None):
    """Leads with at least one unread inbound WhatsApp message."""
    from apps.channels.models import WhatsAppMessage

    messages = WhatsAppMessage.objects.filter(
        organization=organization,
        direction=WhatsAppMessage.Direction.INBOUND,
        is_read=False,
        lead__isnull=False,
    )
    if pipeline_ids:
        messages = messages.filter(lead__pipeline_id__in=pipeline_ids)
    return messages.values("lead_id").distinct().count()


def get_no_reply_24h_count(*, organization, pipeline_ids=None):
    """
    Leads whose most recent WhatsApp message is outbound, sent 24+
    hours ago, with no inbound reply since.
    """
    from apps.channels.models import WhatsAppMessage

    cutoff = timezone.now() - timedelta(hours=24)

    leads = _scope_leads(organization=organization, pipeline_ids=pipeline_ids)

    count = 0
    lead_ids = leads.values_list("id", flat=True)

    # Most recent message per lead, in one query, then filter in Python --
    # a raw "latest message is outbound and old" condition isn't expressible
    # as a single clean ORM filter without a subquery per lead.
    latest_per_lead = (
        WhatsAppMessage.objects.filter(lead_id__in=lead_ids)
        .order_by("lead_id", "-created_at")
        .distinct("lead_id")
        .values("lead_id", "direction", "created_at")
    )

    for row in latest_per_lead:
        if row["direction"] == WhatsAppMessage.Direction.OUTBOUND and row["created_at"] <= cutoff:
            count += 1

    return count


def get_new_leads_trend(*, organization, date_from, date_to, pipeline_ids=None):
    leads = _scope_leads(organization=organization, pipeline_ids=pipeline_ids).filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )

    rows = (
        leads.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )

    return {row["day"]: row["count"] for row in rows}


def get_failed_messages_trend(*, organization, date_from, date_to, pipeline_ids=None):
    from apps.channels.models import WhatsAppMessage

    messages = WhatsAppMessage.objects.filter(
        organization=organization,
        status=WhatsAppMessage.Status.FAILED,
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    if pipeline_ids:
        messages = messages.filter(lead__pipeline_id__in=pipeline_ids)

    rows = (
        messages.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )

    return {row["day"]: row["count"] for row in rows}


def get_leads_by_pipeline(*, organization, pipeline_ids=None):
    leads = _scope_leads(organization=organization, pipeline_ids=pipeline_ids)

    rows = (
        leads.values("pipeline__name")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    return [
        {"label": row["pipeline__name"] or "Unassigned", "count": row["count"]}
        for row in rows
    ]


def get_leads_by_stage(*, organization, pipeline_ids=None):
    leads = _scope_leads(organization=organization, pipeline_ids=pipeline_ids)

    rows = (
        leads.values("stage__name")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    return [
        {"label": row["stage__name"] or "No Stage", "count": row["count"]}
        for row in rows
    ]