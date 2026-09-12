"""Tenant-scoped aggregation helpers for the Insights workspace."""
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.analytics.models import AnalyticsSettings
from apps.crm.models.call import LeadCall
from apps.crm.models.lead import Lead
from apps.crm.models.reminder import LeadReminder


SUCCESS_MESSAGE_STATUSES = ("sent", "delivered", "read")


def _scope_leads(*, organization, pipeline_ids=None, date_from=None, date_to=None):
    qs = Lead.objects.filter(organization=organization)
    if pipeline_ids:
        qs = qs.filter(pipeline_id__in=pipeline_ids)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    return qs


def get_or_create_settings(*, organization):
    settings_obj, _ = AnalyticsSettings.objects.get_or_create(organization=organization)
    return settings_obj


def save_settings(*, organization, hot_lead_stage, lead_won_stage, lead_lost_stage, stall_day_threshold):
    settings_obj = get_or_create_settings(organization=organization)
    settings_obj.hot_lead_stage = hot_lead_stage
    settings_obj.lead_won_stage = lead_won_stage
    settings_obj.lead_lost_stage = lead_lost_stage
    settings_obj.stall_day_threshold = stall_day_threshold
    settings_obj.save()
    return settings_obj


def get_overview_metrics(*, organization, pipeline_ids=None, date_from=None, date_to=None):
    """Top-line metrics used by the Overview cards.

    Existing call metrics remain available for compatibility. Date filtering is
    optional so callers outside the Insights page can continue requesting the
    all-time overview exactly as before.
    """
    from apps.channels.models import WhatsAppAccount, WhatsAppMessage
    from apps.followups.models import FollowupExecution, FollowupStep

    leads = _scope_leads(
        organization=organization,
        pipeline_ids=pipeline_ids,
        date_from=date_from,
        date_to=date_to,
    )
    total_leads = leads.count()

    calls = LeadCall.objects.filter(lead__organization=organization)
    if pipeline_ids:
        calls = calls.filter(lead__pipeline_id__in=pipeline_ids)
    call_totals = calls.aggregate(total_seconds=Sum("duration_seconds"), calls_done=Count("id"))

    messages = WhatsAppMessage.objects.filter(organization=organization)
    if date_from:
        messages = messages.filter(created_at__date__gte=date_from)
    if date_to:
        messages = messages.filter(created_at__date__lte=date_to)
    if pipeline_ids:
        messages = messages.filter(lead__pipeline_id__in=pipeline_ids)

    ai_messages = messages.filter(
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status__in=SUCCESS_MESSAGE_STATUSES,
        raw_payload__shvya_ai__isnull=False,
    ).count()
    welcome_messages = messages.filter(
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status__in=SUCCESS_MESSAGE_STATUSES,
        raw_payload__shvya_welcome__trigger="lead_created",
    ).count()
    failed_templates = messages.filter(
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status=WhatsAppMessage.Status.FAILED,
        account__connection_type=WhatsAppAccount.ConnectionType.API,
        media_payload__transport="template",
    ).count()

    executions = FollowupExecution.objects.filter(
        organization=organization,
        status=FollowupExecution.Status.SENT,
    )
    if date_from:
        executions = executions.filter(updated_at__date__gte=date_from)
    if date_to:
        executions = executions.filter(updated_at__date__lte=date_to)
    if pipeline_ids:
        executions = executions.filter(lead__pipeline_id__in=pipeline_ids)

    total_seconds = call_totals["total_seconds"] or 0
    return {
        "total_leads": total_leads,
        "total_call_minutes": total_seconds // 60,
        "calls_done": call_totals["calls_done"] or 0,
        "ai_messages": ai_messages,
        "welcome_messages": welcome_messages,
        "whatsapp_automation_messages": executions.filter(step__step_type=FollowupStep.StepType.WHATSAPP).count(),
        "email_automation_messages": executions.filter(step__step_type=FollowupStep.StepType.EMAIL).count(),
        "failed_template_messages": failed_templates,
    }


def get_pending_reminder_count(*, organization, pipeline_ids=None):
    reminders = LeadReminder.objects.filter(lead__organization=organization, status="pending")
    if pipeline_ids:
        reminders = reminders.filter(lead__pipeline_id__in=pipeline_ids)
    return reminders.values("lead_id").distinct().count()


def get_pending_replies_count(*, organization, pipeline_ids=None):
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
    from apps.channels.models import WhatsAppMessage

    cutoff = timezone.now() - timedelta(hours=24)
    leads = _scope_leads(organization=organization, pipeline_ids=pipeline_ids)
    latest_per_lead = (
        WhatsAppMessage.objects.filter(lead_id__in=leads.values_list("id", flat=True))
        .order_by("lead_id", "-created_at")
        .distinct("lead_id")
        .values("lead_id", "direction", "created_at")
    )
    return sum(
        1
        for row in latest_per_lead
        if row["direction"] == WhatsAppMessage.Direction.OUTBOUND and row["created_at"] <= cutoff
    )


def get_new_leads_trend(*, organization, date_from, date_to, pipeline_ids=None):
    leads = _scope_leads(
        organization=organization,
        pipeline_ids=pipeline_ids,
        date_from=date_from,
        date_to=date_to,
    )
    rows = (
        leads.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    return {row["day"]: row["count"] for row in rows}


def get_leads_over_time(*, organization, date_from, date_to, pipeline_ids=None):
    """Daily lead creation grouped by the lead's source."""
    leads = _scope_leads(
        organization=organization,
        pipeline_ids=pipeline_ids,
        date_from=date_from,
        date_to=date_to,
    )
    return list(
        leads.annotate(day=TruncDate("created_at"))
        .values("day", "lead_source")
        .annotate(count=Count("id"))
        .order_by("day", "lead_source")
    )


def get_ai_welcome_trend(*, organization, date_from, date_to, pipeline_ids=None):
    """Daily outbound AI and automatic welcome-message activity."""
    from apps.channels.models import WhatsAppMessage

    messages = WhatsAppMessage.objects.filter(
        organization=organization,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status__in=SUCCESS_MESSAGE_STATUSES,
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    if pipeline_ids:
        messages = messages.filter(lead__pipeline_id__in=pipeline_ids)

    rows = (
        messages.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            ai_replies=Count("id", filter=Q(raw_payload__shvya_ai__origin="engagement")),
            ai_bumpups=Count("id", filter=Q(raw_payload__shvya_ai__origin="bump_up")),
            welcome_messages=Count(
                "id",
                filter=Q(raw_payload__shvya_welcome__trigger="lead_created"),
            ),
            total_ai=Count("id", filter=Q(raw_payload__shvya_ai__isnull=False)),
        )
        .order_by("day")
    )
    return list(rows)


def get_automation_flow_trend(*, organization, date_from, date_to, step_type, pipeline_ids=None):
    """Daily sent follow-up executions grouped by sequence name."""
    from apps.followups.models import FollowupExecution

    executions = FollowupExecution.objects.filter(
        organization=organization,
        status=FollowupExecution.Status.SENT,
        step__step_type=step_type,
        updated_at__date__gte=date_from,
        updated_at__date__lte=date_to,
    )
    if pipeline_ids:
        executions = executions.filter(lead__pipeline_id__in=pipeline_ids)

    return list(
        executions.annotate(day=TruncDate("updated_at"))
        .values("day", "sequence__name")
        .annotate(count=Count("id"))
        .order_by("day", "sequence__name")
    )


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


def get_failed_template_messages_queryset(*, organization, date_from=None, date_to=None, pipeline_ids=None):
    """Failed Meta Cloud API template messages visible in Insights."""
    from apps.channels.models import WhatsAppAccount, WhatsAppMessage

    messages = WhatsAppMessage.objects.filter(
        organization=organization,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status=WhatsAppMessage.Status.FAILED,
        account__connection_type=WhatsAppAccount.ConnectionType.API,
        media_payload__transport="template",
    ).select_related("lead", "lead__pipeline", "lead__stage", "account")
    if date_from:
        messages = messages.filter(created_at__date__gte=date_from)
    if date_to:
        messages = messages.filter(created_at__date__lte=date_to)
    if pipeline_ids:
        messages = messages.filter(lead__pipeline_id__in=pipeline_ids)
    return messages.order_by("-created_at")


def get_leads_by_pipeline(*, organization, pipeline_ids=None, date_from=None, date_to=None):
    leads = _scope_leads(
        organization=organization,
        pipeline_ids=pipeline_ids,
        date_from=date_from,
        date_to=date_to,
    )
    rows = leads.values("pipeline__name").annotate(count=Count("id")).order_by("-count")
    return [
        {"label": row["pipeline__name"] or "Unassigned", "count": row["count"]}
        for row in rows
    ]


def get_leads_by_stage(*, organization, pipeline_ids=None, date_from=None, date_to=None):
    leads = _scope_leads(
        organization=organization,
        pipeline_ids=pipeline_ids,
        date_from=date_from,
        date_to=date_to,
    )
    rows = leads.values("stage__name").annotate(count=Count("id")).order_by("-count")
    return [
        {"label": row["stage__name"] or "No Stage", "count": row["count"]}
        for row in rows
    ]
