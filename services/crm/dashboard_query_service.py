"""Query-efficient context builder for the CRM lead dashboard."""

from django.db.models import Count, Prefetch, Q, TextField
from django.db.models.functions import Cast, Upper
from django.utils import timezone

from apps.crm.models import (
    Lead,
    LeadActivity,
    LeadCall,
    LeadNote,
    LeadReminder,
    Pipeline,
    Stage,
)
from apps.crm.views.api import STAGE_THEMES
from services.crm.attribute_cache import get_cached_attribute_definitions


def _apply_core_filters(
    leads_qs,
    *,
    search,
    filter_name,
    filter_phone,
    filter_email,
    filter_notes,
    filter_stage,
):
    if search:
        leads_qs = leads_qs.filter(
            Q(name__icontains=search)
            | Q(phone__icontains=search)
            | Q(email__icontains=search)
        )

    if filter_name:
        leads_qs = leads_qs.filter(name__icontains=filter_name)

    if filter_phone:
        leads_qs = leads_qs.filter(phone__icontains=filter_phone)

    if filter_email:
        leads_qs = leads_qs.filter(email__icontains=filter_email)

    if filter_notes:
        leads_qs = leads_qs.filter(notes__icontains=filter_notes)

    if filter_stage:
        leads_qs = leads_qs.filter(stage_id=filter_stage)

    return leads_qs


def _apply_attribute_filters(leads_qs, request):
    attribute_filters = [
        (key[len("attr_"):], value)
        for key, value in request.GET.items()
        if key.startswith("attr_") and value
    ]

    if not attribute_filters:
        return leads_qs

    # A key-existence predicate can use the JSONB GIN index. The broad
    # text predicate can use the trigram expression index and dramatically
    # narrows the candidate set before the original precise per-key lookup
    # is evaluated. Keep the precise lookup so existing filter semantics do
    # not change.
    leads_qs = leads_qs.alias(
        _attributes_text_upper=Upper(
            Cast("attributes", output_field=TextField())
        )
    )

    for attr_key, value in attribute_filters:
        leads_qs = leads_qs.filter(attributes__has_key=attr_key)

        # jsonb::text escapes quotes and backslashes. Skip the broad
        # accelerator for those uncommon inputs to avoid excluding a valid
        # match; the precise lookup below still handles them correctly.
        if '"' not in value and "\\" not in value:
            leads_qs = leads_qs.filter(
                _attributes_text_upper__contains=value.upper()
            )

        leads_qs = leads_qs.filter(
            **{f"attributes__{attr_key}__icontains": value}
        )

    return leads_qs


def build_lead_table_context(
    request,
    user,
    pipeline,
    active_stage_id="",
):
    """Build lead-table context without per-stage or per-lead queries."""
    organization = user.organization

    search = request.GET.get("search", "").strip()
    filter_name = request.GET.get("filter_name", "").strip()
    filter_phone = request.GET.get("filter_phone", "").strip()
    filter_email = request.GET.get("filter_email", "").strip()
    filter_notes = request.GET.get("filter_notes", "").strip()
    filter_stage = request.GET.get("filter_stage")
    filter_pipeline = request.GET.get("filter_pipeline")

    # Preserve the existing filter-pipeline behavior, but resolve it before
    # building stages/leads so the core filter logic is applied only once.
    if filter_pipeline:
        selected_filter_pipeline = (
            Pipeline.objects
            .filter(
                organization=organization,
                id=filter_pipeline,
                is_active=True,
            )
            .first()
        )
        if selected_filter_pipeline:
            pipeline = selected_filter_pipeline

    stages = list(
        Stage.objects
        .filter(
            pipeline=pipeline,
            is_active=True,
        )
        .order_by("display_order")
    )

    activity_qs = (
        LeadActivity.objects
        .select_related(
            "actor",
            "old_pipeline",
            "new_pipeline",
            "old_stage",
            "new_stage",
        )
        .order_by("-created_at")
    )

    leads_qs = (
        Lead.objects
        .filter(
            organization=organization,
            pipeline=pipeline,
        )
        .select_related(
            "stage",
            "pipeline",
        )
        .annotate(call_count=Count("calls"))
    )

    leads_qs = _apply_core_filters(
        leads_qs,
        search=search,
        filter_name=filter_name,
        filter_phone=filter_phone,
        filter_email=filter_email,
        filter_notes=filter_notes,
        filter_stage=filter_stage,
    )

    leads_qs = _apply_attribute_filters(
        leads_qs,
        request,
    )

    created_after = request.GET.get("filter_created_after")
    if created_after:
        leads_qs = leads_qs.filter(
            created_at__date__gte=created_after
        )

    created_before = request.GET.get("filter_created_before")
    if created_before:
        leads_qs = leads_qs.filter(
            created_at__date__lte=created_before
        )

    leads_qs = leads_qs.prefetch_related(
        Prefetch(
            "calls",
            queryset=LeadCall.objects.order_by("-called_at"),
        ),
        Prefetch(
            "reminders",
            queryset=(
                LeadReminder.objects
                .filter(status="pending")
                .order_by("due_at")
            ),
            to_attr="_pending_reminders_for_card",
        ),
        Prefetch(
            "lead_notes",
            queryset=LeadNote.objects.order_by("-created_at"),
        ),
        Prefetch(
            "activities",
            queryset=activity_qs,
            to_attr="activities_for_card",
        ),
    )

    # One lead query for the complete pipeline. Related card data is loaded
    # by the four batched prefetch queries above, regardless of lead count.
    leads = list(leads_qs)

    attribute_definitions = get_cached_attribute_definitions(
        organization.id
    )

    stage_leads_by_id = {
        stage.id: []
        for stage in stages
    }
    now = timezone.now()

    for lead in leads:
        lead.days_in_stage = (
            now - lead.stage_entered_at
        ).days
        lead.days_in_pipeline = (
            now - lead.created_at
        ).days

        pending_reminders = getattr(
            lead,
            "_pending_reminders_for_card",
            [],
        )
        lead.next_reminder = (
            pending_reminders[0]
            if pending_reminders
            else None
        )

        lead.initials = "".join(
            part[0]
            for part in lead.name.split()[:2]
        ).upper() or "?"

        prefetched_notes = list(
            lead.lead_notes.all()
        )
        latest_note = (
            prefetched_notes[0]
            if prefetched_notes
            else None
        )

        lead.display_note = latest_note

        lead_note_value = (
            lead.notes or ""
        ).strip()
        if lead_note_value:
            lead.display_note_text = lead_note_value
        elif latest_note:
            lead.display_note_text = (
                latest_note.note or ""
            )
        else:
            lead.display_note_text = ""

        # The same organization-level definitions are reused by every card.
        lead.attribute_definitions = attribute_definitions

        # A lead whose stage is no longer active is intentionally omitted,
        # matching the old stage-by-stage query behavior.
        if lead.stage_id in stage_leads_by_id:
            stage_leads_by_id[lead.stage_id].append(lead)

    stage_groups = []
    for index, stage in enumerate(stages):
        stage_leads = stage_leads_by_id[stage.id]
        stage_groups.append(
            {
                "stage": stage,
                "theme": STAGE_THEMES[
                    index % len(STAGE_THEMES)
                ],
                "leads": stage_leads,
                "count": len(stage_leads),
            }
        )

    valid_stage_ids = {
        str(stage.id)
        for stage in stages
    }

    if (
        active_stage_id
        and active_stage_id not in valid_stage_ids
    ):
        active_stage_id = ""

    if not active_stage_id and stages:
        active_stage_id = str(stages[0].id)

    return {
        "stage_groups": stage_groups,
        "all_stages": stages,
        "selected_pipeline_id": str(pipeline.id),
        "active_stage_id": active_stage_id,
    }
