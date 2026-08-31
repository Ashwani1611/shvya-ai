from datetime import timedelta

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.authentication import crm_login_required
from apps.crm.models.pipeline import Pipeline
from apps.crm.models.stage import Stage
from services.analytics.analytics_service import (
    get_failed_messages_trend,
    get_leads_by_pipeline,
    get_leads_by_stage,
    get_new_leads_trend,
    get_no_reply_24h_count,
    get_or_create_settings,
    get_overview_metrics,
    get_pending_reminder_count,
    get_pending_replies_count,
    save_settings,
)


def _can_manage(user):
    return user.is_superuser or user.role in (
        User.Role.SUPERADMIN,
        User.Role.ADMIN,
    )


def _parse_pipeline_ids(request):
    raw = request.GET.getlist("pipeline")
    return [p for p in raw if p] or None


def _parse_date_range(request):
    today = timezone.localdate()
    default_from = today - timedelta(days=7)

    date_from = request.GET.get("date_from") or default_from.isoformat()
    date_to = request.GET.get("date_to") or today.isoformat()

    return date_from, date_to


def _fill_trend(trend_by_day, date_from, date_to):
    """Turn a {date: count} dict into an ordered list covering every day
    in the range, so the chart doesn't skip days with zero activity."""
    from datetime import date as date_cls

    start = date_cls.fromisoformat(str(date_from))
    end = date_cls.fromisoformat(str(date_to))

    labels = []
    values = []

    current = start
    while current <= end:
        labels.append(current.strftime("%d %b"))
        values.append(trend_by_day.get(current, 0))
        current += timedelta(days=1)

    return labels, values


@crm_login_required
def analytics_dashboard_view(request):
    user = request.crm_user
    organization = user.organization

    pipeline_ids = _parse_pipeline_ids(request)
    date_from, date_to = _parse_date_range(request)

    pipelines = Pipeline.objects.filter(organization=organization, is_active=True).order_by("name")

    overview = get_overview_metrics(organization=organization, pipeline_ids=pipeline_ids)
    pending_reminder_count = get_pending_reminder_count(organization=organization, pipeline_ids=pipeline_ids)
    pending_replies_count = get_pending_replies_count(organization=organization, pipeline_ids=pipeline_ids)
    no_reply_24h_count = get_no_reply_24h_count(organization=organization, pipeline_ids=pipeline_ids)

    new_leads_trend = get_new_leads_trend(
        organization=organization, date_from=date_from, date_to=date_to, pipeline_ids=pipeline_ids,
    )
    failed_messages_trend = get_failed_messages_trend(
        organization=organization, date_from=date_from, date_to=date_to, pipeline_ids=pipeline_ids,
    )

    new_leads_labels, new_leads_values = _fill_trend(new_leads_trend, date_from, date_to)
    failed_messages_labels, failed_messages_values = _fill_trend(failed_messages_trend, date_from, date_to)

    leads_by_pipeline = get_leads_by_pipeline(organization=organization, pipeline_ids=pipeline_ids)
    leads_by_stage = get_leads_by_stage(organization=organization, pipeline_ids=pipeline_ids)

    settings_obj = get_or_create_settings(organization=organization)

    return render(
        request,
        "analytics/dashboard.html",
        {
            "pipelines": pipelines,
            "selected_pipeline_ids": pipeline_ids or [],
            "date_from": date_from,
            "date_to": date_to,
            "overview": overview,
            "pending_reminder_count": pending_reminder_count,
            "pending_replies_count": pending_replies_count,
            "no_reply_24h_count": no_reply_24h_count,
            "new_leads_labels": new_leads_labels,
            "new_leads_values": new_leads_values,
            "failed_messages_labels": failed_messages_labels,
            "failed_messages_values": failed_messages_values,
            "leads_by_pipeline": leads_by_pipeline,
            "leads_by_stage": leads_by_stage,
            "settings_obj": settings_obj,
            "can_manage": _can_manage(user),
        },
    )


@crm_login_required
def analytics_settings_view(request):
    user = request.crm_user
    organization = user.organization

    if not _can_manage(user):
        messages.error(request, "Only org admins can change analytics settings.")
        return redirect("crm-analytics")

    settings_obj = get_or_create_settings(organization=organization)

    if request.method == "POST":
        hot_lead_stage_id = request.POST.get("hot_lead_stage")
        lead_won_stage_id = request.POST.get("lead_won_stage")
        lead_lost_stage_id = request.POST.get("lead_lost_stage")
        stall_day_threshold = request.POST.get("stall_day_threshold") or 7

        stages = Stage.objects.filter(pipeline__organization=organization)

        hot_lead_stage = stages.filter(id=hot_lead_stage_id).first() if hot_lead_stage_id else None
        lead_won_stage = stages.filter(id=lead_won_stage_id).first() if lead_won_stage_id else None
        lead_lost_stage = stages.filter(id=lead_lost_stage_id).first() if lead_lost_stage_id else None

        try:
            stall_day_threshold = int(stall_day_threshold)
        except (TypeError, ValueError):
            stall_day_threshold = 7

        save_settings(
            organization=organization,
            hot_lead_stage=hot_lead_stage,
            lead_won_stage=lead_won_stage,
            lead_lost_stage=lead_lost_stage,
            stall_day_threshold=stall_day_threshold,
        )

        messages.success(request, "Analytics settings saved.")
        return redirect("crm-analytics")

    stages_by_pipeline = Stage.objects.filter(
        pipeline__organization=organization,
    ).select_related("pipeline").order_by("pipeline__name", "display_order")

    return render(
        request,
        "analytics/settings_modal.html",
        {
            "settings_obj": settings_obj,
            "stages_by_pipeline": stages_by_pipeline,
        },
    )