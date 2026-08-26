import json
import logging
from datetime import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, Max
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import (
    Lead,
    LeadCall,
    LeadNote,
    LeadReminder,
    Pipeline,
    Stage,
)

from .api import STAGE_THEMES, get_user_pipelines


logger = logging.getLogger(__name__)


# ============================================================
# DASHBOARD
# ============================================================


@crm_login_required
def dashboard_view(request):

    user = request.crm_user

    pending_reminder_count = (
        LeadReminder.objects
        .filter(
            lead__organization=user.organization,
            status="pending",
        )
        .count()
    )

    pipelines = get_user_pipelines(
        user
    )

    requested_pipeline_id = request.GET.get(
        "pipeline"
    )

    selected_pipeline = None

    # --------------------------------------------------------
    # ADMIN
    #
    # Admins can access every active pipeline in their
    # organization.
    #
    # Default pipeline is always "Leads" when it exists.
    # --------------------------------------------------------

    if user.role == User.Role.ADMIN:

        if requested_pipeline_id:

            selected_pipeline = (
                pipelines
                .filter(
                    id=requested_pipeline_id,
                )
                .first()
            )

        if selected_pipeline is None:

            selected_pipeline = (
                pipelines
                .filter(
                    name="Leads",
                )
                .first()
            )

        if selected_pipeline is None:

            selected_pipeline = (
                pipelines.first()
            )

    # --------------------------------------------------------
    # AGENT
    #
    # Agents are restricted to their assigned/owned
    # pipeline.
    #
    # A requested pipeline ID is ignored if it is not the
    # Agent's permitted pipeline.
    # --------------------------------------------------------

    elif user.role == User.Role.AGENT:

        selected_pipeline = (
            pipelines.first()
        )

    selected_pipeline_id = (
        selected_pipeline.id
        if selected_pipeline
        else None
    )

    response = render(
        request,
        "crm/dashboard.html",
        {
            "pipelines": pipelines,

            "selected_pipeline_id": (
                str(
                    selected_pipeline_id
                )
                if selected_pipeline_id
                else None
            ),

            "crm_user": user,

            "pending_reminder_count":
                pending_reminder_count,
        },
    )

    response["Cache-Control"] = (
        "no-cache, no-store, must-revalidate"
    )

    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


# ============================================================
# INTERNAL LEAD TABLE CONTEXT BUILDER
# ============================================================


def _build_lead_table_context(
    request,
    user,
    pipeline,
    active_stage_id="",
):
    """
    Build the complete context required by lead_table.html.

    This reuses the existing CRM lead/filter logic so that
    normal table requests and stage-management refreshes render
    the same Lead Cards and stage panels.

    IMPORTANT:
        This function does not modify Lead Card behavior.
    """

    organization = user.organization

    search = request.GET.get(
        "search",
        "",
    ).strip()

    stages = (
        Stage.objects
        .filter(
            pipeline=pipeline,
            is_active=True,
        )
        .order_by(
            "display_order"
        )
    )

    leads_qs = (
        Lead.objects
        .filter(
            organization=organization,
            pipeline=pipeline,
        )
        .select_related(
            "stage"
        )
    )

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    if search:

        leads_qs = leads_qs.filter(
            Q(
                name__icontains=search
            )
            | Q(
                phone__icontains=search
            )
            | Q(
                email__icontains=search
            )
        )

    # --------------------------------------------------------
    # CORE FILTERS
    # --------------------------------------------------------

    filter_name = request.GET.get(
        "filter_name",
        "",
    ).strip()

    if filter_name:

        leads_qs = leads_qs.filter(
            name__icontains=filter_name
        )

    filter_phone = request.GET.get(
        "filter_phone",
        "",
    ).strip()

    if filter_phone:

        leads_qs = leads_qs.filter(
            phone__icontains=filter_phone
        )

    filter_email = request.GET.get(
        "filter_email",
        "",
    ).strip()

    if filter_email:

        leads_qs = leads_qs.filter(
            email__icontains=filter_email
        )

    filter_notes = request.GET.get(
        "filter_notes",
        "",
    ).strip()

    if filter_notes:

        leads_qs = leads_qs.filter(
            notes__icontains=filter_notes
        )

    filter_stage = request.GET.get(
        "filter_stage"
    )

    if filter_stage:

        leads_qs = leads_qs.filter(
            stage_id=filter_stage
        )

    # --------------------------------------------------------
    # FILTER PIPELINE
    #
    # This preserves the existing filter behavior.
    # --------------------------------------------------------

    filter_pipeline = request.GET.get(
        "filter_pipeline"
    )

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

            pipeline = (
                selected_filter_pipeline
            )

            stages = (
                Stage.objects
                .filter(
                    pipeline=pipeline,
                    is_active=True,
                )
                .order_by(
                    "display_order"
                )
            )

            leads_qs = (
                Lead.objects
                .filter(
                    organization=organization,
                    pipeline=pipeline,
                )
                .select_related(
                    "stage"
                )
            )

            if search:

                leads_qs = leads_qs.filter(
                    Q(
                        name__icontains=search
                    )
                    | Q(
                        phone__icontains=search
                    )
                    | Q(
                        email__icontains=search
                    )
                )

            if filter_name:

                leads_qs = leads_qs.filter(
                    name__icontains=filter_name
                )

            if filter_phone:

                leads_qs = leads_qs.filter(
                    phone__icontains=filter_phone
                )

            if filter_email:

                leads_qs = leads_qs.filter(
                    email__icontains=filter_email
                )

            if filter_notes:

                leads_qs = leads_qs.filter(
                    notes__icontains=filter_notes
                )

            if filter_stage:

                leads_qs = leads_qs.filter(
                    stage_id=filter_stage
                )

    # --------------------------------------------------------
    # ATTRIBUTE FILTERS
    # --------------------------------------------------------

    for key, value in request.GET.items():

        if (
            key.startswith(
                "attr_"
            )
            and value
        ):

            attr_key = key[
                len("attr_"):
            ]

            leads_qs = leads_qs.filter(
                **{
                    f"attributes__{attr_key}__icontains":
                    value
                }
            )

    # --------------------------------------------------------
    # CREATED DATE
    # --------------------------------------------------------

    created_after = request.GET.get(
        "filter_created_after"
    )

    if created_after:

        leads_qs = leads_qs.filter(
            created_at__date__gte=created_after
        )

    created_before = request.GET.get(
        "filter_created_before"
    )

    if created_before:

        leads_qs = leads_qs.filter(
            created_at__date__lte=created_before
        )

    # --------------------------------------------------------
    # STAGE GROUPS
    # --------------------------------------------------------

    stage_groups = []

    for i, stage in enumerate(
        stages
    ):

        stage_leads = list(
            leads_qs.filter(
                stage=stage
            )
        )

        for lead in stage_leads:

            lead.days_in_stage = (
                timezone.now()
                - lead.updated_at
            ).days

            lead.days_in_pipeline = (
                timezone.now()
                - lead.created_at
            ).days

            lead.call_count = (
                lead.calls.count()
            )

            lead.next_reminder = (
                lead.reminders
                .filter(
                    status="pending"
                )
                .order_by(
                    "due_at"
                )
                .first()
            )

            lead.initials = "".join(
                [
                    p[0]
                    for p in lead.name.split()[:2]
                ]
            ).upper() or "?"

            lead_note_value = ""

            if hasattr(
                lead,
                "notes",
            ):

                lead_note_value = (
                    lead.notes or ""
                ).strip()

            latest_note = (
                LeadNote.objects
                .filter(
                    lead=lead
                )
                .order_by(
                    "-created_at"
                )
                .first()
            )

            lead.display_note = (
                latest_note
            )

            if lead_note_value:

                lead.display_note_text = (
                    lead_note_value
                )

            elif latest_note:

                lead.display_note_text = (
                    latest_note.note or ""
                )

            else:

                lead.display_note_text = ""

        theme = STAGE_THEMES[
            i % len(STAGE_THEMES)
        ]

        stage_groups.append(
            {
                "stage": stage,
                "theme": theme,
                "leads": stage_leads,
                "count": len(stage_leads),
            }
        )

    # --------------------------------------------------------
    # ACTIVE STAGE
    #
    # The active stage is UI state only.
    # It does NOT filter the lead queryset.
    #
    # That is important because the stage panels must each
    # retain their own complete lead lists.
    # --------------------------------------------------------

    valid_stage_ids = {
        str(stage.id)
        for stage in stages
    }

    if (
        active_stage_id
        and active_stage_id not in valid_stage_ids
    ):

        active_stage_id = ""

    if (
        not active_stage_id
        and stages.exists()
    ):

        active_stage_id = str(
            stages.first().id
        )

    return {
        "stage_groups": stage_groups,

        "all_stages": stages,

        "selected_pipeline_id": str(
            pipeline.id
        ),

        "active_stage_id":
            active_stage_id,
    }


# ============================================================
# LEAD TABLE
# ============================================================


@crm_login_required
@require_GET
def lead_table_partial(
    request
):

    user = request.crm_user

    organization = user.organization

    pipeline_id = request.GET.get(
        "pipeline"
    )

    active_stage_id = (
        request.GET.get(
            "stage",
            "",
        )
        .strip()
    )

    if (
        not pipeline_id
        or pipeline_id == "None"
    ):

        return render(
            request,
            "crm/partials/lead_table.html",
            {
                "stage_groups": [],
                "all_stages": [],
                "selected_pipeline_id": "",
                "active_stage_id": "",
            },
        )

    pipeline = (
        get_user_pipelines(
            user
        )
        .filter(
            id=pipeline_id,
        )
        .first()
    )

    if not pipeline:

        return render(
            request,
            "crm/partials/lead_table.html",
            {
                "stage_groups": [],
                "all_stages": [],
                "selected_pipeline_id": "",
                "active_stage_id": "",
            },
        )

    context = _build_lead_table_context(
        request=request,
        user=user,
        pipeline=pipeline,
        active_stage_id=active_stage_id,
    )

    return render(
        request,
        "crm/partials/lead_table.html",
        context,
    )


# ============================================================
# CREATE STAGE
# ============================================================

@crm_login_required
@require_POST
def lead_stage_create(
    request,
):

    user = request.crm_user

    organization = user.organization


    pipeline_id = (
        request.POST.get(
            "pipeline",
            "",
        )
        .strip()
    )


    active_stage_id = (
        request.POST.get(
            "active_stage",
            "",
        )
        .strip()
    )


    name = (
        request.POST.get(
            "name",
            "",
        )
        .strip()
    )


    if not pipeline_id:

        return HttpResponse(
            "Pipeline is required.",
            status=400,
        )


    if not name:

        return HttpResponse(
            "Stage name is required.",
            status=400,
        )


    # --------------------------------------------------------
    # ACCESSIBLE PIPELINE
    # --------------------------------------------------------

    pipeline = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=organization,
            id=pipeline_id,
            is_active=True,
        )
        .first()
    )


    if not pipeline:

        return HttpResponse(
            "Pipeline not accessible.",
            status=403,
        )


    # --------------------------------------------------------
    # DUPLICATE NAME
    # --------------------------------------------------------

    if (
        Stage.objects
        .filter(
            pipeline=pipeline,
            is_active=True,
            name__iexact=name,
        )
        .exists()
    ):

        return HttpResponse(
            "A stage with this name already exists.",
            status=400,
        )


    # --------------------------------------------------------
    # DISPLAY ORDER
    # --------------------------------------------------------

    max_order = (
        Stage.objects
        .filter(
            pipeline=pipeline,
        )
        .aggregate(
            max_order=Max(
                "display_order"
            )
        )
        .get(
            "max_order"
        )
    )


    Stage.objects.create(
        pipeline=pipeline,
        name=name,
        display_order=(
            (max_order or 0) + 1
        ),
        is_active=True,
    )


    # --------------------------------------------------------
    # KEEP SELECTED STAGE
    # --------------------------------------------------------

    context = _build_lead_table_context(
        request=request,
        user=user,
        pipeline=pipeline,
        active_stage_id=active_stage_id,
    )


    return render(
        request,
        "crm/partials/lead_table.html",
        context,
    )


# ============================================================
# RENAME STAGE
# ============================================================

@crm_login_required
@require_POST
def lead_stage_rename(
    request,
    stage_id,
):

    user = request.crm_user

    organization = user.organization


    name = (
        request.POST.get(
            "name",
            "",
        )
        .strip()
    )


    active_stage_id = (
        request.POST.get(
            "active_stage",
            "",
        )
        .strip()
    )


    if not name:

        return HttpResponse(
            "Stage name is required.",
            status=400,
        )


# ============================================================
# DELETE STAGE
# ============================================================


@crm_login_required
@require_POST
def lead_stage_delete(
    request,
    stage_id,
):

    user = request.crm_user

    organization = user.organization

    pipeline_id = (
        request.POST.get(
            "pipeline",
            "",
        )
        .strip()
    )

    active_stage_id = (
        request.POST.get(
            "active_stage",
            "",
        )
        .strip()
    )


    # --------------------------------------------------------
    # LOAD ONLY A STAGE THE USER CAN ACCESS
    # --------------------------------------------------------

    allowed_pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=organization,
            is_active=True,
        )
    )


    stage = (
        Stage.objects
        .filter(
            id=stage_id,
            pipeline__in=allowed_pipelines,
            is_active=True,
        )
        .select_related(
            "pipeline",
        )
        .first()
    )


    if not stage:

        return HttpResponse(
            "Stage not found.",
            status=404,
        )


    # --------------------------------------------------------
    # PROTECT LEADS
    #
    # Do not delete a stage while leads are still assigned
    # to it.
    # --------------------------------------------------------

    has_leads = (
        Lead.objects
        .filter(
            organization=organization,
            pipeline=stage.pipeline,
            stage=stage,
        )
        .exists()
    )


    if has_leads:

        return HttpResponse(
            (
                "This stage cannot be deleted because "
                "it still contains leads. Move the leads "
                "to another stage first."
            ),
            status=409,
        )


    # --------------------------------------------------------
    # SOFT DELETE
    #
    # Existing architecture already uses is_active=True
    # when displaying stages, so this safely removes the
    # stage from the CRM UI without physically deleting it.
    # --------------------------------------------------------

    stage.is_active = False

    stage.save(
        update_fields=[
            "is_active",
        ]
    )


    # --------------------------------------------------------
    # IF THE DELETED STAGE WAS ACTIVE, CLEAR THE ACTIVE STAGE
    # --------------------------------------------------------

    if (
        active_stage_id
        == str(stage.id)
    ):

        active_stage_id = ""


    # --------------------------------------------------------
    # REBUILD THE CURRENT TABLE
    # --------------------------------------------------------

    context = _build_lead_table_context(
        request=request,
        user=user,
        pipeline=stage.pipeline,
        active_stage_id=active_stage_id,
    )


    return render(
        request,
        "crm/partials/lead_table.html",
        context,
    )

    # --------------------------------------------------------
    # ALLOWED PIPELINES
    # --------------------------------------------------------

    allowed_pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=organization,
            is_active=True,
        )
    )


    # --------------------------------------------------------
    # LOAD STAGE
    # --------------------------------------------------------

    stage = (
        Stage.objects
        .filter(
            id=stage_id,
            pipeline__in=allowed_pipelines,
            is_active=True,
        )
        .select_related(
            "pipeline",
        )
        .first()
    )


    if not stage:

        return HttpResponse(
            "Stage not found.",
            status=404,
        )


    # --------------------------------------------------------
    # DUPLICATE NAME
    # --------------------------------------------------------

    if (
        Stage.objects
        .filter(
            pipeline=stage.pipeline,
            is_active=True,
            name__iexact=name,
        )
        .exclude(
            id=stage.id,
        )
        .exists()
    ):

        return HttpResponse(
            "A stage with this name already exists.",
            status=400,
        )


    # --------------------------------------------------------
    # RENAME
    # --------------------------------------------------------

    stage.name = name

    stage.save(
        update_fields=[
            "name",
        ]
    )


    # --------------------------------------------------------
    # RENDER UPDATED TABLE
    #
    # No redirect.
    # No browser navigation.
    # Only #lead-table-container is replaced.
    # --------------------------------------------------------

    context = _build_lead_table_context(
        request=request,
        user=user,
        pipeline=stage.pipeline,
        active_stage_id=(
            active_stage_id
            or str(stage.id)
        ),
    )


    return render(
        request,
        "crm/partials/lead_table.html",
        context,
    )


# ============================================================
# RENAME STAGE
# ============================================================


@crm_login_required
@require_POST
def lead_stage_rename(
    request,
    stage_id,
):

    user = request.crm_user

    organization = user.organization


    name = (
        request.POST.get(
            "name",
            "",
        )
        .strip()
    )

    active_stage_id = (
        request.POST.get(
            "active_stage",
            "",
        )
        .strip()
    )


    if not name:

        return HttpResponse(
            "Stage name is required.",
            status=400,
        )


    # --------------------------------------------------------
    # PIPELINE ACCESS
    # --------------------------------------------------------

    allowed_pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=organization,
            is_active=True,
        )
    )


    stage = (
        Stage.objects
        .filter(
            id=stage_id,
            pipeline__in=allowed_pipelines,
            is_active=True,
        )
        .select_related(
            "pipeline",
        )
        .first()
    )


    if not stage:

        return HttpResponse(
            "Stage not found.",
            status=404,
        )


    # --------------------------------------------------------
    # DUPLICATE NAME
    # --------------------------------------------------------

    if (
        Stage.objects
        .filter(
            pipeline=stage.pipeline,
            is_active=True,
            name__iexact=name,
        )
        .exclude(
            id=stage.id,
        )
        .exists()
    ):

        return HttpResponse(
            "A stage with this name already exists.",
            status=400,
        )


    # --------------------------------------------------------
    # RENAME
    # --------------------------------------------------------

    stage.name = name

    stage.save(
        update_fields=[
            "name",
        ]
    )


    # --------------------------------------------------------
    # RENDER UPDATED TABLE PARTIAL
    #
    # IMPORTANT:
    # No redirect.
    # No browser navigation.
    # --------------------------------------------------------

    context = _build_lead_table_context(
        request=request,
        user=user,
        pipeline=stage.pipeline,
        active_stage_id=(
            active_stage_id
            or str(stage.id)
        ),
    )


    return render(
        request,
        "crm/partials/lead_table.html",
        context,
    )


# ============================================================
# LEAD DETAIL
# ============================================================


@crm_login_required
@require_GET
def lead_detail(
    request,
    lead_id,
):

    user = request.crm_user

    organization = user.organization

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=organization,
    )

    calls = (
        lead.calls
        .order_by("-created_at")
    )

    notes = (
        LeadNote.objects
        .filter(
            lead=lead,
        )
        .order_by("-created_at")
    )

    reminders = (
        lead.reminders
        .order_by("-due_at")
    )

    contacts = (
        lead.contacts
        .all()
    )

    stages = (
        Stage.objects
        .filter(
            pipeline=lead.pipeline,
            is_active=True,
        )
        .order_by("display_order")
    )

    initials = "".join(
        [
            part[0]
            for part in lead.name.split()[:2]
        ]
    ).upper() or "?"

    lead_note_text = (
        lead.notes or ""
    ).strip()

    return render(
        request,
        "crm/partials/lead_detail.html",
        {
            "lead": lead,
            "calls": calls,
            "notes": notes,
            "reminders": reminders,
            "contacts": contacts,
            "stages": stages,
            "initials": initials,
            "lead_note_text": lead_note_text,
        },
    )


# ============================================================
# LEAD CARD ACTIONS
# ============================================================


def _lead_card_context(
    lead,
    user,
):
    """
    Build the reusable context required by the Lead Card.

    This keeps card rendering consistent across:

        - initial dashboard rendering
        - call updates
        - reminder updates
        - note updates
        - attribute updates

    IMPORTANT:
        Keep this isolated from stage-management logic.
    """

    lead.days_in_stage = (
        timezone.now()
        - lead.updated_at
    ).days

    lead.days_in_pipeline = (
        timezone.now()
        - lead.created_at
    ).days

    lead.call_count = (
        lead.calls.count()
    )

    lead.next_reminder = (
        lead.reminders
        .filter(
            status="pending",
        )
        .order_by(
            "due_at",
        )
        .first()
    )

    lead.initials = "".join(
        [
            part[0]
            for part in lead.name.split()[:2]
        ]
    ).upper() or "?"

    latest_note = (
        LeadNote.objects
        .filter(
            lead=lead,
        )
        .order_by(
            "-created_at",
        )
        .first()
    )

    lead.display_note = (
        latest_note
    )

    lead.display_note_text = (
        lead.notes or ""
    ).strip()

    if (
        not lead.display_note_text
        and latest_note
    ):

        lead.display_note_text = (
            latest_note.note or ""
        )

    return {
        "lead": lead,
        "user": user,

        "calls": (
            lead.calls
            .order_by(
                "-called_at",
            )
        ),

        "reminders": (
            lead.reminders
            .order_by(
                "due_at",
            )
        ),

        "notes": (
            lead.lead_notes
            .order_by(
                "-created_at",
            )
        ),
    }


@crm_login_required
@require_GET
def lead_card_partial(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    context = _lead_card_context(
        lead,
        user,
    )

    context["all_stages"] = (
        Stage.objects
        .filter(
            pipeline=lead.pipeline,
            is_active=True,
        )
        .order_by(
            "display_order",
        )
    )

    return render(
        request,
        "crm/partials/lead_card.html",
        context,
    )

@crm_login_required
@require_POST
def lead_stage_move(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    # --------------------------------------------------------
    # READ TARGET STAGE
    # --------------------------------------------------------

    stage_id = (
        request.POST.get(
            "stage_id",
            "",
        )
        or request.POST.get(
            "stage",
            "",
        )
    ).strip()

    if not stage_id:

        return HttpResponse(
            "Stage is required.",
            status=400,
        )

    # --------------------------------------------------------
    # TARGET STAGE
    # Must belong to the same pipeline.
    # --------------------------------------------------------

    stage = (
        Stage.objects
        .filter(
            id=stage_id,
            pipeline=lead.pipeline,
            is_active=True,
        )
        .first()
    )

    if not stage:

        return HttpResponse(
            "Invalid stage for this lead.",
            status=400,
        )

    old_stage_id = (
        str(lead.stage_id)
        if lead.stage_id
        else ""
    )

    new_stage_id = str(
        stage.id
    )

    # --------------------------------------------------------
    # NO CHANGE
    # --------------------------------------------------------

    if old_stage_id == new_stage_id:

        response = HttpResponse("")

        response["HX-Trigger"] = json.dumps(
            {
                "leadStageUpdated": {
                    "lead_id": str(lead.id),
                    "old_stage_id": old_stage_id,
                    "stage_id": new_stage_id,
                    "pipeline_id": str(
                        lead.pipeline_id
                    ),
                }
            }
        )

        return response

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    try:

        lead.stage = stage

        lead.save(
            update_fields=[
                "stage",
                "updated_at",
            ]
        )

    except DjangoValidationError as e:

        logger.exception(
            "Lead stage move validation failed "
            "for lead %s",
            lead_id,
        )

        return HttpResponse(
            f"Validation error: {e}",
            status=400,
        )

    except Exception as e:

        logger.exception(
            "Lead stage move failed "
            "for lead %s",
            lead_id,
        )

        return HttpResponse(
            f"Error moving lead: {e}",
            status=400,
        )

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadStageUpdated": {
                "lead_id": str(
                    lead.id
                ),
                "old_stage_id": old_stage_id,
                "stage_id": new_stage_id,
                "pipeline_id": str(
                    lead.pipeline_id
                ),
            }
        }
    )

    return response

# ============================================================
# ADD CALL
# ============================================================


@crm_login_required
@require_GET
def lead_call_modal(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    return render(
        request,
        "crm/partials/lead_call_modal.html",
        {
            "lead": lead,
        },
    )


@crm_login_required
@require_POST
def lead_call_save(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    status = request.POST.get(
        "status",
        "completed",
    ).strip()

    allowed_statuses = {
        choice[0]
        for choice in LeadCall._meta.get_field(
            "status"
        ).choices
    }

    if status not in allowed_statuses:

        return HttpResponse(
            "Invalid call status.",
            status=400,
        )

    duration_seconds_raw = request.POST.get(
        "duration_seconds",
        "0",
    ).strip()

    try:

        duration_seconds = int(
            duration_seconds_raw or 0
        )

    except ValueError:

        return HttpResponse(
            "Invalid duration.",
            status=400,
        )

    if duration_seconds < 0:

        return HttpResponse(
            "Duration cannot be negative.",
            status=400,
        )

    notes = request.POST.get(
        "notes",
        "",
    ).strip()

    called_at_raw = request.POST.get(
        "called_at",
        "",
    ).strip()

    if called_at_raw:

        try:

            called_at = datetime.fromisoformat(
                called_at_raw
            )

            if timezone.is_naive(
                called_at
            ):

                called_at = (
                    timezone.make_aware(
                        called_at,
                        timezone.get_current_timezone(),
                    )
                )

        except ValueError:

            return HttpResponse(
                "Invalid call date/time.",
                status=400,
            )

    else:

        called_at = timezone.now()

    LeadCall.objects.create(
        lead=lead,
        user=user,
        status=status,
        duration_seconds=duration_seconds,
        notes=notes,
        called_at=called_at,
    )

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadCardUpdated": {
                "lead_id": str(
                    lead.id
                )
            }
        }
    )

    return response


# ============================================================
# ADD REMINDER
# ============================================================


@crm_login_required
@require_GET
def lead_reminder_modal(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    return render(
        request,
        "crm/partials/lead_reminder_modal.html",
        {
            "lead": lead,
        },
    )


@crm_login_required
@require_POST
def lead_reminder_save(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    title = request.POST.get(
        "title",
        "",
    ).strip()

    description = request.POST.get(
        "description",
        "",
    ).strip()

    due_at_raw = request.POST.get(
        "due_at",
        "",
    ).strip()

    if not title:

        return HttpResponse(
            "Reminder title is required.",
            status=400,
        )

    if not due_at_raw:

        return HttpResponse(
            "Reminder date/time is required.",
            status=400,
        )

    try:

        due_at = datetime.fromisoformat(
            due_at_raw
        )

        if timezone.is_naive(
            due_at
        ):

            due_at = (
                timezone.make_aware(
                    due_at,
                    timezone.get_current_timezone(),
                )
            )

    except ValueError:

        return HttpResponse(
            "Invalid reminder date/time.",
            status=400,
        )

    LeadReminder.objects.create(
        lead=lead,
        assigned_to=user,
        title=title,
        description=description,
        due_at=due_at,
        status="pending",
    )

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadCardUpdated": {
                "lead_id": str(
                    lead.id
                )
            }
        }
    )

    return response


# ============================================================
# ADD NOTE
# ============================================================


@crm_login_required
@require_GET
def lead_note_modal(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    return render(
        request,
        "crm/partials/lead_note_modal.html",
        {
            "lead": lead,
        },
    )


@crm_login_required
@require_POST
def lead_note_save(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    note = request.POST.get(
        "note",
        "",
    ).strip()

    if not note:

        return HttpResponse(
            "Note cannot be empty.",
            status=400,
        )

    LeadNote.objects.create(
        lead=lead,
        created_by=user,
        note=note,
        note_type="manual",
    )

    lead.notes = note

    lead.save(
        update_fields=[
            "notes",
            "updated_at",
        ]
    )

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadCardUpdated": {
                "lead_id": str(
                    lead.id
                )
            }
        }
    )

    return response


# ============================================================
# PHASE 3 PART B — EDIT LEAD MODAL
# ============================================================


@crm_login_required
@require_GET
def lead_edit_modal(
    request,
    lead_id,
):

    user = request.crm_user

    organization = user.organization

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=organization,
    )

    pipelines = get_user_pipelines(
        user
    )

    stages = (
        Stage.objects
        .filter(
            pipeline=lead.pipeline,
            is_active=True,
        )
        .order_by(
            "display_order"
        )
    )

    latest_note = (
        LeadNote.objects
        .filter(
            lead=lead,
        )
        .order_by(
            "-created_at",
        )
        .first()
    )

    lead_note_text = ""

    if hasattr(
        lead,
        "notes",
    ):

        lead_note_text = (
            lead.notes or ""
        ).strip()

    if (
        not lead_note_text
        and latest_note
    ):

        lead_note_text = (
            latest_note.note or ""
        )

    return render(
        request,
        "crm/partials/lead_edit_modal.html",
        {
            "lead": lead,
            "pipelines": pipelines,
            "stages": stages,
            "latest_note": latest_note,
            "lead_note_text": lead_note_text,
        },
    )


# ============================================================
# PHASE 3 PART B — EDIT LEAD STAGE OPTIONS
# ============================================================


@crm_login_required
@require_GET
def lead_edit_stages(
    request,
):

    user = request.crm_user

    organization = user.organization

    pipeline_id = request.GET.get(
        "pipeline",
        "",
    ).strip()

    allowed_pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=organization,
            is_active=True,
        )
    )

    pipeline = get_object_or_404(
        allowed_pipelines,
        id=pipeline_id,
    )

    stages = (
        Stage.objects
        .filter(
            pipeline=pipeline,
            is_active=True,
        )
        .order_by(
            "display_order",
        )
    )

    return render(
        request,
        "crm/partials/lead_edit_stages.html",
        {
            "stages": stages,
        },
    )


# ============================================================
# SAVE LEAD EDIT
# ============================================================


@crm_login_required
@require_POST
def lead_edit_save(
    request,
    lead_id,
):

    user = request.crm_user

    organization = user.organization

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=organization,
    )

    name = request.POST.get(
        "name",
        "",
    ).strip()

    email = request.POST.get(
        "email",
        "",
    ).strip()

    phone = request.POST.get(
        "phone",
        "",
    ).strip()

    pipeline_id = request.POST.get(
        "pipeline",
        "",
    ).strip()

    stage_id = request.POST.get(
        "stage",
        "",
    ).strip()

    notes = request.POST.get(
        "notes",
        "",
    ).strip()

    allowed_pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=organization,
            is_active=True,
        )
    )

    if pipeline_id:

        pipeline = get_object_or_404(
            allowed_pipelines,
            id=pipeline_id,
        )

        lead.pipeline = pipeline

    if stage_id:

        stage = get_object_or_404(
            Stage,
            id=stage_id,
            pipeline=lead.pipeline,
            is_active=True,
        )

        lead.stage = stage

    lead.name = (
        name
        or lead.name
    )

    lead.email = email

    if phone:

        lead.phone = phone

    else:

        lead.phone = ""

    if hasattr(
        lead,
        "notes",
    ):

        lead.notes = notes

    # --------------------------------------------------------
    # ATTRIBUTES
    # --------------------------------------------------------

    attributes = dict(
        lead.attributes or {}
    )

    for key, value in request.POST.items():

        if key.startswith(
            "attr_"
        ):

            attr_key = key[
                len("attr_"):
            ]

            attributes[attr_key] = value

    lead.attributes = attributes

    try:

        lead.full_clean()

        lead.save()

        latest_manual_note = (
            LeadNote.objects
            .filter(
                lead=lead,
                note_type="manual",
            )
            .order_by(
                "-created_at"
            )
            .first()
        )

        if notes:

            if latest_manual_note:

                latest_manual_note.note = (
                    notes
                )

                latest_manual_note.save(
                    update_fields=[
                        "note",
                        "updated_at",
                    ]
                )

            else:

                LeadNote.objects.create(
                    lead=lead,
                    created_by=user,
                    note=notes,
                    note_type="manual",
                )

        else:

            if latest_manual_note:

                latest_manual_note.delete()

    except DjangoValidationError as e:

        logger.exception(
            "Lead edit validation failed "
            "for lead %s",
            lead_id,
        )

        error_message = (
            e.message_dict
            if hasattr(
                e,
                "message_dict",
            )
            else e.messages
        )

        return HttpResponse(
            f"""
            <div class="text-red-600 text-sm p-4">
                Validation error: {error_message}
            </div>
            """,
            status=400,
        )

    except Exception as e:

        logger.exception(
            "Lead edit save failed "
            "for lead %s",
            lead_id,
        )

        return HttpResponse(
            f"""
            <div class="text-red-600 text-sm p-4">
                Error saving lead: {e}
            </div>
            """,
            status=400,
        )

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadCardUpdated": {
                "lead_id": str(
                    lead.id
                )
            }
        }
    )

    return response


# ============================================================
# FILTER CONFIGURATION
# ============================================================


CORE_FILTER_FIELDS = [
    "Name",
    "Phone",
    "Email",
    "Notes",
    "Stage",
    "Pipeline",
]


DATE_FILTER_FIELDS = [
    "Created Date",
    "Reminder Date",
    "Stage Updated Date",
    "Pipeline Updated Date",
    "AI Qualified Date",
    "Days in stage",
]


# ============================================================
# LEAD FILTER MODAL
# ============================================================


@crm_login_required
@require_GET
def lead_filters_modal(
    request
):

    user = request.crm_user

    organization = user.organization

    pipeline_id = request.GET.get(
        "pipeline"
    )

    attribute_keys = set()

    for attrs in (
        Lead.objects
        .filter(
            organization=organization
        )
        .values_list(
            "attributes",
            flat=True,
        )
    ):

        if attrs:

            attribute_keys.update(
                attrs.keys()
            )

    return render(
        request,
        "crm/partials/lead_filters_modal.html",
        {
            "core_fields": (
                CORE_FILTER_FIELDS
            ),

            "attribute_fields": sorted(
                attribute_keys
            ),

            "date_fields": (
                DATE_FILTER_FIELDS
            ),

            "pipeline_id": pipeline_id,
        },
    )


# ============================================================
# FILTER VALUES
# ============================================================


@crm_login_required
@require_GET
def lead_filters_values(
    request
):

    fields = request.GET.getlist(
        "fields"
    )

    pipeline_id = request.GET.get(
        "pipeline"
    )

    user = request.crm_user

    organization = user.organization

    stages = (
        Stage.objects
        .filter(
            pipeline_id=pipeline_id,
            pipeline__organization=organization,
            is_active=True,
        )
        if pipeline_id
        else []
    )

    pipelines = (
        Pipeline.objects
        .filter(
            organization=organization,
            is_active=True,
        )
    )

    return render(
        request,
        "crm/partials/lead_filters_values.html",
        {
            "fields": fields,
            "stages": stages,
            "pipelines": pipelines,
            "pipeline_id": pipeline_id,
        },
    )