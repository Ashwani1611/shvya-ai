import uuid

from uuid import UUID

from django.db.models import Count, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead, LeadActivity, LeadCall, LeadNote, LeadReminder, Stage
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
from .bulk import bulk_campaign_available, bulk_permissions


def _pipeline_entered_at(lead):
    for activity in lead.activities_for_card:
        if (
            activity.topic == LeadActivity.Topic.PIPELINE_CHANGED
            and activity.new_pipeline_id == lead.pipeline_id
        ):
            return activity.created_at
    return lead.created_at


def _prepare_lead(lead, attribute_definitions, now):
    lead.days_in_stage = max(0, (now - lead.stage_entered_at).days)
    lead.days_in_pipeline = max(0, (now - _pipeline_entered_at(lead)).days)
    pending_reminders = lead.pending_reminders_for_card
    lead.next_reminder = pending_reminders[0] if pending_reminders else None
    lead.initials = "".join(part[0] for part in lead.name.split()[:2]).upper() or "?"

    notes = list(lead.lead_notes.all())
    latest_note = notes[0] if notes else None
    lead.display_note = latest_note
    lead.display_note_text = (lead.notes or "").strip()
    if not lead.display_note_text and latest_note:
        lead.display_note_text = (latest_note.note or "").strip()

    lead.attribute_definitions = attribute_definitions
    lead.has_conversation_summary = True
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

    current_pipeline = (
        pipelines.filter(id=requested_pipeline_id).first()
        if requested_pipeline_id
        else None
    )
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
    requested_stage = str(request.GET.get("stage") or "").strip()
    filter_stage = str(request.GET.get("filter_stage") or "").strip()
    valid_stage_ids = {str(stage.id) for stage in stages}
    active_stage_id = filter_stage if filter_stage in valid_stage_ids else requested_stage
    if active_stage_id not in valid_stage_ids:
        active_stage_id = str(stages[0].id) if stages else ""

    # Count matching leads across stages without building every card.
    counts = dict(
        queryset.filter(stage__in=stages)
        .order_by()
        .values("stage_id")
        .annotate(total=Count("pk", distinct=True))
        .values_list("stage_id", "total")
    )
    has_current_matches = any(counts.values())
    stage_groups = [
        {
            "stage": stage,
            "theme": STAGE_THEMES[index % len(STAGE_THEMES)],
            "leads": [],
            "count": counts.get(stage.id, 0),
            "query": query_with(
                request.GET, pipeline=current_pipeline.id, stage=stage.id,
                filter_stage=None, page=None,
            ),
        }
        for index, stage in enumerate(stages)
    ]

    page_size = 40
    active_stage = next(
        (stage for stage in stages if str(stage.id) == active_stage_id), None
    )
    active_count = counts.get(active_stage.id, 0) if active_stage else 0
    total_pages = max(1, (active_count + page_size - 1) // page_size)
    try:
        page_number = int(request.GET.get("page") or 1)
    except (TypeError, ValueError):
        page_number = 1
    page_number = min(max(1, page_number), total_pages)
    # A dashboard link can point to a lead beyond page one. Locate its page
    # within this filtered stage before rendering the first batch.
    requested_lead_id = str(request.GET.get("lead") or "").strip()
    if active_stage is not None and requested_lead_id and not request.GET.get("page"):
        try:
            requested_lead_id = uuid.UUID(requested_lead_id)
        except (TypeError, ValueError, AttributeError):
            requested_lead_id = None
        if requested_lead_id is not None:
            target = queryset.filter(
                stage=active_stage, pk=requested_lead_id,
            ).values("created_at", "pk").first()
            if target is not None:
                preceding = queryset.filter(stage=active_stage).filter(
                    Q(created_at__gt=target["created_at"])
                    | Q(created_at=target["created_at"], pk__gt=target["pk"])
                ).count()
                page_number = preceding // page_size + 1

    if active_stage is not None and active_count:
        # Only visible cards need scoring, attachments, history or notes.
        leads = list(
            queryset.filter(stage=active_stage)
            .annotate(call_count=Count("calls", distinct=True))
            .order_by("-created_at", "-pk")
            .prefetch_related(
                Prefetch("lead_notes", queryset=LeadNote.objects.order_by("-created_at")),
                Prefetch("calls", queryset=LeadCall.objects.order_by("-called_at")),
                Prefetch(
                    "reminders",
                    queryset=LeadReminder.objects.filter(status="pending").order_by("due_at"),
                    to_attr="pending_reminders_for_card",
                ),
                Prefetch(
                    "activities",
                    queryset=LeadActivity.objects.select_related(
                        "actor", "old_pipeline", "new_pipeline", "old_stage", "new_stage",
                    ).order_by("-created_at"),
                    to_attr="activities_for_card",
                ),
            )[(page_number - 1) * page_size:page_number * page_size]
        )
        attribute_definitions = list(public_attribute_definitions(user.organization))
        from apps.ai_engagement.services.intent_score import prepare_intent_scores
        prepare_intent_scores(leads)
        from apps.shvya_calendar.services import attach_calendar_attachments_to_leads
        attach_calendar_attachments_to_leads(leads, organization=user.organization)
        now = timezone.now()
        for lead in leads:
            _prepare_lead(lead, attribute_definitions, now)
        for group in stage_groups:
            if group["stage"].id == active_stage.id:
                group["leads"] = leads
                break

    context["lead_page"] = {
        "number": page_number,
        "total_pages": total_pages,
        "start": (page_number - 1) * page_size + 1 if active_count else 0,
        "end": min(page_number * page_size, active_count),
        "total": active_count,
        "previous_query": query_with(
            request.GET, pipeline=current_pipeline.id, stage=active_stage_id,
            filter_stage=None, page=page_number - 1,
        ) if page_number > 1 else "",
        "next_query": query_with(
            request.GET, pipeline=current_pipeline.id, stage=active_stage_id,
            filter_stage=None, page=page_number + 1,
        ) if page_number < total_pages else "",
    }

    matches = []
    if not has_current_matches and has_active_filters(request.GET):
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
            "pipeline_lead_count": Lead.objects.filter(
                organization=user.organization, pipeline=current_pipeline,
            ).count(),
            "bulk_campaign_available": bulk_campaign_available(user, current_pipeline),
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


@crm_login_required
@require_GET
def lead_stage_counts(request):
    """Current filtered stage totals after client-side card mutations."""
    user = request.crm_user
    try:
        pipeline_id = UUID(str(request.GET.get("pipeline") or ""))
    except (TypeError, ValueError, AttributeError):
        return JsonResponse({"error": "Pipeline not found."}, status=404)
    pipeline = accessible_pipelines(user).filter(id=pipeline_id).first()
    if pipeline is None:
        return JsonResponse({"error": "Pipeline not found."}, status=404)
    stages = list(Stage.objects.filter(
        pipeline=pipeline, is_active=True,
    ).values_list("id", flat=True))
    queryset = apply_lead_filters(
        Lead.objects.filter(organization=user.organization, pipeline=pipeline),
        request.GET, user=user, include_search=True,
    )
    counts = dict(
        queryset.filter(stage_id__in=stages)
        .order_by()
        .values("stage_id")
        .annotate(total=Count("pk", distinct=True))
        .values_list("stage_id", "total")
    )
    return JsonResponse({
        "counts": {str(stage_id): counts.get(stage_id, 0) for stage_id in stages},
    })


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
            "selected_filter_stage": str(request.GET.get("filter_stage") or ""),
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
            "total_count": (
                len(overdue_reminders)
                + len(today_reminders)
                + len(upcoming_reminders)
            ),
            "active_filters": active_filter_items(request.GET, user=user),
            "filters_query": request.GET.urlencode(),
        },
    )
