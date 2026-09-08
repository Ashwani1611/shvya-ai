from datetime import datetime

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from apps.crm.models import AttributeDefinition, Lead, LeadNote, LeadReminder, Stage
from services.crm.lead_filter_service import (
    accessible_pipelines,
    active_filter_items,
    apply_lead_filters,
    cross_pipeline_matches,
    has_active_filters,
    public_attribute_definitions,
    query_with,
)

from .api import STAGE_THEMES
from .bulk import bulk_permissions


def _pipeline_entered_at(lead):
    activity = (
        lead.activities.filter(
            topic="pipeline_changed",
            new_pipeline_id=lead.pipeline_id,
        )
        .order_by("-created_at")
        .first()
    )
    return activity.created_at if activity else lead.created_at


def _prepare_lead(lead, attribute_definitions):
    now = timezone.now()
    lead.days_in_stage = max(0, (now - lead.stage_entered_at).days)
    lead.days_in_pipeline = max(0, (now - _pipeline_entered_at(lead)).days)
    lead.call_count = lead.calls.count()
    lead.next_reminder = (
        lead.reminders.filter(status="pending").order_by("due_at").first()
    )
    lead.initials = "".join(part[0] for part in lead.name.split()[:2]).upper() or "?"

    latest_note = (
        LeadNote.objects.filter(lead=lead).order_by("-created_at").first()
    )
    lead.display_note = latest_note
    lead.display_note_text = (lead.notes or "").strip()
    if not lead.display_note_text and latest_note:
        lead.display_note_text = (latest_note.note or "").strip()

    lead.activities_for_card = (
        lead.activities.select_related(
            "actor",
            "old_pipeline",
            "new_pipeline",
            "old_stage",
            "new_stage",
        ).order_by("-created_at")
    )
    lead.attribute_definitions = attribute_definitions
    return lead


def _base_filter_context(request, user):
    active = active_filter_items(request.GET, user=user)
    return {
        "active_filters": active,
        "filters_active": bool(active),
    }


@crm_login_required
@require_GET
def lead_table_partial(request):
    """Filtered CRM table with working cross-pipeline filtering."""
    user = request.crm_user
    pipelines = accessible_pipelines(user)
    requested_pipeline_id = str(request.GET.get("pipeline") or "").strip()
    filter_pipeline = str(request.GET.get("filter_pipeline") or "").strip()

    current_pipeline = pipelines.filter(id=requested_pipeline_id).first()
    if current_pipeline is None:
        current_pipeline = pipelines.first()

    context = _base_filter_context(request, user)
    context.update(
        {
            "cross_pipeline_matches": [],
            "all_pipeline_mode": filter_pipeline == "all",
            "all_pipeline_results": [],
            "selected_pipeline_id": str(current_pipeline.id) if current_pipeline else "",
            "active_stage_id": "",
            "stage_groups": [],
            "all_stages": [],
            "bulk_query": request.GET.urlencode(),
        }
    )

    if filter_pipeline == "all":
        queryset = Lead.objects.filter(
            organization=user.organization,
            pipeline__in=pipelines,
        ).select_related("pipeline", "stage")
        queryset = apply_lead_filters(
            queryset,
            request.GET,
            user=user,
            include_search=True,
        ).order_by("pipeline__name", "stage__display_order", "-created_at")
        results = list(queryset[:500])
        for lead in results:
            lead.initials = "".join(
                part[0] for part in lead.name.split()[:2]
            ).upper() or "?"
        context["all_pipeline_results"] = results
        return render(request, "crm/partials/lead_table_filtered.html", context)

    if filter_pipeline:
        selected = pipelines.filter(id=filter_pipeline).first()
        if selected:
            current_pipeline = selected
            context["selected_pipeline_id"] = str(selected.id)

    if not current_pipeline:
        return render(request, "crm/partials/lead_table_filtered.html", context)

    stages = list(
        Stage.objects.filter(
            pipeline=current_pipeline,
            is_active=True,
        ).order_by("display_order")
    )
    queryset = Lead.objects.filter(
        organization=user.organization,
        pipeline=current_pipeline,
    ).select_related("pipeline", "stage")
    queryset = apply_lead_filters(
        queryset,
        request.GET,
        user=user,
        include_search=True,
    )

    attribute_definitions = list(public_attribute_definitions(user.organization))
    stage_groups = []
    for index, stage in enumerate(stages):
        stage_leads = list(queryset.filter(stage=stage))
        for lead in stage_leads:
            _prepare_lead(lead, attribute_definitions)
        stage_groups.append(
            {
                "stage": stage,
                "theme": STAGE_THEMES[index % len(STAGE_THEMES)],
                "leads": stage_leads,
                "count": len(stage_leads),
            }
        )

    requested_stage = str(request.GET.get("stage") or "").strip()
    filter_stage = str(request.GET.get("filter_stage") or "").strip()
    valid_stage_ids = {str(stage.id) for stage in stages}
    active_stage_id = filter_stage if filter_stage in valid_stage_ids else requested_stage
    if active_stage_id not in valid_stage_ids:
        active_stage_id = str(stages[0].id) if stages else ""

    matches = cross_pipeline_matches(
        request.GET,
        user=user,
        current_pipeline=current_pipeline,
    )
    for item in matches:
        item["query"] = query_with(
            request.GET,
            pipeline=item["pipeline"].id,
            filter_pipeline=item["pipeline"].id,
            stage=None,
            filter_stage=None,
        )

    context.update(
        {
            "stage_groups": stage_groups,
            "all_stages": stages,
            "selected_pipeline_id": str(current_pipeline.id),
            "active_stage_id": active_stage_id,
            "bulk_permissions": bulk_permissions(user, current_pipeline),
            "cross_pipeline_matches": matches,
            "all_pipelines_query": query_with(
                request.GET,
                filter_pipeline="all",
                stage=None,
                filter_stage=None,
            ),
        }
    )
    return render(request, "crm/partials/lead_table_filtered.html", context)


def _filter_surface(request):
    surface = str(request.GET.get("surface") or "crm").strip().lower()
    return surface if surface in {"crm", "whatsapp", "reminders"} else "crm"


@crm_login_required
@require_GET
def lead_filters_modal(request):
    """One CRM filter UI shared by CRM, WhatsApp chats, and reminders."""
    user = request.crm_user
    surface = _filter_surface(request)
    pipelines = list(accessible_pipelines(user))
    stages = list(
        Stage.objects.filter(
            pipeline__in=pipelines,
            is_active=True,
        )
        .select_related("pipeline")
        .order_by("pipeline__name", "display_order")
    )
    definitions = list(public_attribute_definitions(user.organization))
    for definition in definitions:
        definition.current_filter_value = str(
            request.GET.get(f"attr_{definition.key}") or ""
        )

    if surface == "whatsapp":
        action_url = reverse("whatsapp-chats")
        target = "#wa-web-shell"
        swap = "outerHTML"
    elif surface == "reminders":
        action_url = reverse("crm-global-reminders-modal")
        target = "#modal-root"
        swap = "innerHTML"
    else:
        action_url = reverse("crm-lead-table-partial")
        target = "#lead-table-container"
        swap = "innerHTML"

    preserve = []
    if surface == "crm":
        pipeline = str(request.GET.get("pipeline") or "").strip()
        if pipeline:
            preserve.append(("pipeline", pipeline))
        search = str(request.GET.get("search") or "").strip()
        if search:
            preserve.append(("search", search))
    elif surface == "whatsapp":
        for key in ("account", "tab", "q"):
            value = str(request.GET.get(key) or "").strip()
            if value:
                preserve.append((key, value))

    return render(
        request,
        "crm/partials/lead_filters_modal_v2.html",
        {
            "surface": surface,
            "filter_action_url": action_url,
            "filter_target": target,
            "filter_swap": swap,
            "pipelines": pipelines,
            "stages": stages,
            "attribute_definitions": definitions,
            "preserve_params": preserve,
            "selected_filter_pipeline": str(
                request.GET.get("filter_pipeline") or ""
            ),
            "selected_filter_stage": str(
                request.GET.get("filter_stage") or ""
            ),
        },
    )


@crm_login_required
@require_GET
def global_reminders_modal(request):
    """Render actual pending reminders with the same CRM lead filters."""
    user = request.crm_user
    filtered_leads = Lead.objects.filter(organization=user.organization)
    filtered_leads = apply_lead_filters(
        filtered_leads,
        request.GET,
        user=user,
        include_search=True,
    )
    reminders = (
        LeadReminder.objects.filter(
            lead__organization=user.organization,
            lead__in=filtered_leads,
            status="pending",
        )
        .select_related("lead", "lead__pipeline", "lead__stage")
        .order_by("due_at")
    )

    now = timezone.localtime(timezone.now())
    overdue_reminders = []
    today_reminders = []
    upcoming_reminders = []
    for reminder in reminders:
        due = timezone.localtime(reminder.due_at)
        if due < now:
            overdue_reminders.append(reminder)
        elif due.date() == now.date():
            today_reminders.append(reminder)
        else:
            upcoming_reminders.append(reminder)

    return render(
        request,
        "crm/partials/global_reminders_modal_v2.html",
        {
            "overdue_reminders": overdue_reminders,
            "today_reminders": today_reminders,
            "upcoming_reminders": upcoming_reminders,
            "overdue_count": len(overdue_reminders),
            "today_count": len(today_reminders),
            "upcoming_count": len(upcoming_reminders),
            "total_count": len(overdue_reminders) + len(today_reminders) + len(upcoming_reminders),
            "active_filters": active_filter_items(request.GET, user=user),
            "filters_query": request.GET.urlencode(),
        },
    )
