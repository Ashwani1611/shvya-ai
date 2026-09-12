import csv
from datetime import date as date_cls, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.authentication import crm_login_required
from apps.crm.models.lead import Lead
from apps.crm.models.pipeline import Pipeline
from apps.crm.models.stage import Stage
from apps.followups.models import FollowupStep
from services.analytics.analytics_service import (
    get_ai_welcome_trend,
    get_automation_flow_trend,
    get_failed_template_messages_queryset,
    get_leads_by_pipeline,
    get_leads_by_stage,
    get_leads_over_time,
    get_or_create_settings,
    get_overview_metrics,
    save_settings,
)


def _can_manage(user):
    return user.is_superuser or user.role in (User.Role.SUPERADMIN, User.Role.ADMIN)


def _parse_pipeline_ids(request):
    raw = request.GET.getlist("pipeline")
    return [p for p in raw if p] or None


def _parse_date_range(request):
    today = timezone.localdate()
    default_from = today - timedelta(days=28)

    try:
        start = date_cls.fromisoformat(request.GET.get("date_from") or default_from.isoformat())
    except (TypeError, ValueError):
        start = default_from
    try:
        end = date_cls.fromisoformat(request.GET.get("date_to") or today.isoformat())
    except (TypeError, ValueError):
        end = today

    if start > end:
        start, end = end, start
    # Keep a dashboard request bounded while still allowing year-over-year views.
    if (end - start).days > 366:
        start = end - timedelta(days=366)
    return start.isoformat(), end.isoformat()


def _date_axis(date_from, date_to):
    start = date_cls.fromisoformat(str(date_from))
    end = date_cls.fromisoformat(str(date_to))
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days, [day.strftime("%b %d") for day in days]


def _grouped_series(rows, *, date_from, date_to, group_key, label_map=None):
    days, labels = _date_axis(date_from, date_to)
    buckets = {}
    for row in rows:
        group = row.get(group_key) or "Unassigned"
        buckets.setdefault(str(group), {})[row["day"]] = row["count"]

    series = []
    for group in sorted(buckets, key=lambda value: str(label_map.get(value, value) if label_map else value).lower()):
        series.append(
            {
                "key": group,
                "label": label_map.get(group, group) if label_map else group,
                "values": [buckets[group].get(day, 0) for day in days],
            }
        )
    return labels, series


def _metric_series(rows, *, date_from, date_to):
    days, labels = _date_axis(date_from, date_to)
    by_day = {row["day"]: row for row in rows}
    definitions = (
        ("ai_replies", "AI Replies"),
        ("ai_bumpups", "Bump Up Messages"),
        ("welcome_messages", "Welcome Messages"),
        ("total_ai", "Total AI Messages"),
    )
    series = [
        {
            "key": key,
            "label": label,
            "values": [int((by_day.get(day) or {}).get(key, 0) or 0) for day in days],
        }
        for key, label in definitions
    ]
    return labels, series


@crm_login_required
def analytics_dashboard_view(request):
    user = request.crm_user
    organization = user.organization
    pipeline_ids = _parse_pipeline_ids(request)
    date_from, date_to = _parse_date_range(request)

    pipelines = Pipeline.objects.filter(organization=organization, is_active=True).order_by("name")
    overview = get_overview_metrics(
        organization=organization,
        pipeline_ids=pipeline_ids,
        date_from=date_from,
        date_to=date_to,
    )

    source_labels = dict(Lead._meta.get_field("lead_source").choices)
    leads_labels, leads_series = _grouped_series(
        get_leads_over_time(
            organization=organization,
            date_from=date_from,
            date_to=date_to,
            pipeline_ids=pipeline_ids,
        ),
        date_from=date_from,
        date_to=date_to,
        group_key="lead_source",
        label_map=source_labels,
    )
    ai_labels, ai_series = _metric_series(
        get_ai_welcome_trend(
            organization=organization,
            date_from=date_from,
            date_to=date_to,
            pipeline_ids=pipeline_ids,
        ),
        date_from=date_from,
        date_to=date_to,
    )
    email_labels, email_series = _grouped_series(
        get_automation_flow_trend(
            organization=organization,
            date_from=date_from,
            date_to=date_to,
            step_type=FollowupStep.StepType.EMAIL,
            pipeline_ids=pipeline_ids,
        ),
        date_from=date_from,
        date_to=date_to,
        group_key="sequence__name",
    )
    whatsapp_labels, whatsapp_series = _grouped_series(
        get_automation_flow_trend(
            organization=organization,
            date_from=date_from,
            date_to=date_to,
            step_type=FollowupStep.StepType.WHATSAPP,
            pipeline_ids=pipeline_ids,
        ),
        date_from=date_from,
        date_to=date_to,
        group_key="sequence__name",
    )

    leads_by_pipeline = get_leads_by_pipeline(
        organization=organization,
        pipeline_ids=pipeline_ids,
        date_from=date_from,
        date_to=date_to,
    )
    leads_by_stage = get_leads_by_stage(
        organization=organization,
        pipeline_ids=pipeline_ids,
        date_from=date_from,
        date_to=date_to,
    )

    failed_qs = get_failed_template_messages_queryset(
        organization=organization,
        date_from=date_from,
        date_to=date_to,
        pipeline_ids=pipeline_ids,
    )
    failed_page = Paginator(failed_qs, 25).get_page(request.GET.get("page") or 1)
    approved_templates = (
        WhatsAppTemplate.objects.filter(
            organization=organization,
            status=WhatsAppTemplate.Status.APPROVED,
            account__connection_type=WhatsAppAccount.ConnectionType.API,
            account__is_active=True,
        )
        .select_related("account")
        .order_by("account__business_name", "name")
    )

    return render(
        request,
        "analytics/dashboard.html",
        {
            "pipelines": pipelines,
            "selected_pipeline_ids": pipeline_ids or [],
            "date_from": date_from,
            "date_to": date_to,
            "overview": overview,
            "leads_labels": leads_labels,
            "leads_series": leads_series,
            "ai_labels": ai_labels,
            "ai_series": ai_series,
            "email_labels": email_labels,
            "email_series": email_series,
            "whatsapp_labels": whatsapp_labels,
            "whatsapp_series": whatsapp_series,
            "leads_by_pipeline": leads_by_pipeline,
            "leads_by_stage": leads_by_stage,
            "failed_page": failed_page,
            "approved_templates": approved_templates,
            "can_manage": _can_manage(user),
        },
    )


@crm_login_required
@require_POST
def retry_failed_template_messages_view(request):
    from apps.channels.tasks import send_whatsapp_message_task
    from services.channels.whatsapp_template_delivery import (
        WhatsAppTemplateSendError,
        queue_template_message,
    )

    user = request.crm_user
    organization = user.organization
    message_ids = [value for value in request.POST.getlist("message_ids") if value]
    if not message_ids and request.POST.get("message_id"):
        message_ids = [request.POST["message_id"]]
    if not message_ids:
        messages.error(request, "Select at least one failed WhatsApp message to retry.")
        return redirect("crm-analytics")

    failed_messages = list(
        get_failed_template_messages_queryset(organization=organization).filter(id__in=message_ids)
    )
    if not failed_messages:
        messages.error(request, "No retryable failed template messages were found.")
        return redirect("crm-analytics")

    explicit_template = None
    template_id = (request.POST.get("template_id") or "").strip()
    if template_id:
        explicit_template = (
            WhatsAppTemplate.objects.filter(
                id=template_id,
                organization=organization,
                status=WhatsAppTemplate.Status.APPROVED,
                account__connection_type=WhatsAppAccount.ConnectionType.API,
                account__is_active=True,
            )
            .select_related("account")
            .first()
        )
        if not explicit_template:
            messages.error(request, "Choose an approved WhatsApp API template.")
            return redirect("crm-analytics")

    queued = 0
    skipped = 0
    seen_leads = set()
    for failed in failed_messages:
        if not failed.lead_id or failed.lead_id in seen_leads:
            skipped += 1
            continue
        seen_leads.add(failed.lead_id)

        template = explicit_template
        if template is None:
            payload = failed.media_payload if isinstance(failed.media_payload, dict) else {}
            original_template_id = payload.get("template_id")
            template = (
                WhatsAppTemplate.objects.filter(
                    id=original_template_id,
                    organization=organization,
                    status=WhatsAppTemplate.Status.APPROVED,
                    account__connection_type=WhatsAppAccount.ConnectionType.API,
                    account__is_active=True,
                )
                .select_related("account")
                .first()
                if original_template_id
                else None
            )
        if template is None:
            skipped += 1
            continue

        try:
            queued_message = queue_template_message(template=template, lead=failed.lead, user=user)
        except WhatsAppTemplateSendError:
            skipped += 1
            continue
        send_whatsapp_message_task.delay(str(queued_message.id))
        queued += 1

    if queued:
        messages.success(request, f"{queued} template message{'s' if queued != 1 else ''} queued for retry.")
    if skipped:
        messages.warning(request, f"{skipped} selected item{'s' if skipped != 1 else ''} could not be retried.")

    next_url = request.POST.get("next") or ""
    if next_url.startswith("/dashboard/insights/"):
        return redirect(next_url)
    return redirect("crm-analytics")


@crm_login_required
def export_failed_template_leads_view(request):
    user = request.crm_user
    pipeline_ids = _parse_pipeline_ids(request)
    date_from, date_to = _parse_date_range(request)
    failed_messages = get_failed_template_messages_queryset(
        organization=user.organization,
        date_from=date_from,
        date_to=date_to,
        pipeline_ids=pipeline_ids,
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="failed-whatsapp-templates-{date_from}-to-{date_to}.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "Lead Name",
            "Phone",
            "Email",
            "Pipeline",
            "Stage",
            "Template",
            "Failure Reason",
            "Failed At",
            "Message ID",
        ]
    )
    for message in failed_messages.iterator():
        lead = message.lead
        payload = message.media_payload if isinstance(message.media_payload, dict) else {}
        writer.writerow(
            [
                lead.name if lead else "",
                lead.phone if lead else message.to_number,
                lead.email if lead else "",
                lead.pipeline.name if lead and lead.pipeline_id else "",
                lead.stage.name if lead and lead.stage_id else "",
                payload.get("template_name") or "",
                message.error or "Meta rejected the delivery.",
                timezone.localtime(message.created_at).isoformat(),
                str(message.id),
            ]
        )
    return response


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

    stages_by_pipeline = Stage.objects.filter(pipeline__organization=organization).select_related("pipeline").order_by(
        "pipeline__name", "display_order"
    )
    return render(
        request,
        "analytics/settings_modal.html",
        {"settings_obj": settings_obj, "stages_by_pipeline": stages_by_pipeline},
    )
