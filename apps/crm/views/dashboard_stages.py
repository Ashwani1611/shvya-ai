# ruff: noqa: F401
"""Stage-management views extracted from the CRM dashboard compatibility module."""

import json
import logging
from datetime import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Max
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.db import transaction

from services.crm_activity_service import (
    record_stage_changed,
    record_pipeline_changed,
    record_reminder_created,
    record_note_added,
    record_call_logged,
)

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import (
    Lead,
    LeadCall,
    LeadNote,
    LeadReminder,
    Pipeline,
    Stage,
    AttributeDefinition,
)

from services.crm.lead_service import (
    create_lead,
)

from services.crm.dashboard_query_service import (
    build_lead_table_context as _build_lead_table_context,
)
from .api import get_user_pipelines
from . import filtering as _filtering_views
from . import lead_import as _lead_import_views
from . import reminders as _reminder_views

# Compatibility aliases: implementations were split into focused modules, but
# callers that historically imported these names from dashboard.py remain valid.
global_reminders_modal = _filtering_views.global_reminders_modal
lead_filters_modal = _filtering_views.lead_filters_modal
lead_table_partial = _filtering_views.lead_table_partial

lead_import_start_modal = _lead_import_views.lead_import_start_modal
lead_import_start = _lead_import_views.lead_import_start
lead_import_upload_modal = _lead_import_views.lead_import_upload_modal
lead_import_upload = _lead_import_views.lead_import_upload
lead_import_sample_file = _lead_import_views.lead_import_sample_file
lead_import_mapping_modal = _lead_import_views.lead_import_mapping_modal
lead_import_mapping_save = _lead_import_views.lead_import_mapping_save
lead_import_destination_modal = _lead_import_views.lead_import_destination_modal
lead_import_destination_save = _lead_import_views.lead_import_destination_save
lead_import_review_modal = _lead_import_views.lead_import_review_modal
lead_import_execute = _lead_import_views.lead_import_execute

reminder_notification_feed = _reminder_views.reminder_notification_feed
reminder_notification_ack = _reminder_views.reminder_notification_ack
global_reminder_complete = _reminder_views.global_reminder_complete
global_reminder_snooze = _reminder_views.global_reminder_snooze
global_reminder_delete = _reminder_views.global_reminder_delete
global_reminder_edit_modal = _reminder_views.global_reminder_edit_modal
global_reminder_edit_save = _reminder_views.global_reminder_edit_save


logger = logging.getLogger(__name__)


# ============================================================
# DASHBOARD
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
# TOGGLE STAGE AI
# ============================================================

@crm_login_required
@require_POST
def lead_stage_ai_toggle(
    request,
    stage_id,
):

    user = request.crm_user

    organization = user.organization

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
    # TOGGLE AI
    # --------------------------------------------------------

    stage.ai_on = not stage.ai_on

    stage.save(
        update_fields=[
            "ai_on",
        ]
    )

    return render(
        request,
        "crm/partials/stage_ai_toggle.html",
        {"stage": stage},
    )

def _stage_entry_prompt(request, lead, stage, errors=None):
    from apps.crm.services.stage_requirements import required_attributes
    fields = list(required_attributes(stage))
    for field in fields:
        field.entry_value = request.POST.get(f"attr_{field.key}", (lead.attributes or {}).get(field.key, ""))
    response = render(request, "crm/partials/stage_entry_modal.html", {
        "lead": lead, "target_stage": stage, "entry_fields": fields,
        "entry_errors": errors or [], "entry_action": request.path,
        "entry_payload": [(key, value) for key, value in request.POST.items()
                          if key != "csrfmiddlewaretoken" and not key.startswith("attr_")],
        "other_attributes": [(key, value) for key, value in request.POST.items()
                             if key.startswith("attr_") and key[5:] not in {a.key for a in fields}],
    })
    response["HX-Retarget"] = "#modal-root"
    response["HX-Reswap"] = "innerHTML"
    return response

@crm_login_required
@require_POST
@transaction.atomic
def lead_stage_move(
    request,
    lead_id,
):

    user = request.crm_user

    lead = get_object_or_404(
        Lead.objects.select_for_update(),
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
    #
    # The new stage must:
    #   - exist
    #   - be active
    #   - belong to the Lead's current pipeline
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

    # --------------------------------------------------------
    # CAPTURE OLD STATE BEFORE MUTATION
    # --------------------------------------------------------

    old_stage_id = (
        str(lead.stage_id)
        if lead.stage_id
        else ""
    )

    new_stage_id = str(
        stage.id
    )

    old_stage = lead.stage
    pipeline = lead.pipeline

    # --------------------------------------------------------
    # NO CHANGE
    # --------------------------------------------------------

    if old_stage_id == new_stage_id:

        response = HttpResponse("")

        response["HX-Trigger"] = json.dumps(
            {
                "leadStageUpdated": {
                    "lead_id": str(
                        lead.id
                    ),
                    "old_stage_id": (
                        old_stage_id
                    ),
                    "stage_id": (
                        new_stage_id
                    ),
                    "pipeline_id": str(
                        lead.pipeline_id
                    ),
                }
            }
        )

        return response

    from apps.crm.services.stage_requirements import clean_entry_values, missing_attributes
    values, errors = clean_entry_values(stage, lead.attributes, request.POST)
    if errors or missing_attributes(stage, values):
        return _stage_entry_prompt(request, lead, stage, errors)
    lead.attributes = values

    # --------------------------------------------------------
    # SAVE LEAD + ACTIVITY ATOMICALLY
    # --------------------------------------------------------

    try:

        with transaction.atomic():

            # ------------------------------------------------
            # MOVE LEAD
            # ------------------------------------------------

            lead.stage = stage
            lead.stage_entered_at = timezone.now()

            lead.save(
                update_fields=[
                    "attributes",
                    "stage",
                    "stage_entered_at",
                    "updated_at",
                ]
            )

            # ------------------------------------------------
            # CREATE PERMANENT ACTIVITY
            #
            # IMPORTANT:
            # old_stage and pipeline were captured BEFORE
            # changing the Lead.
            # ------------------------------------------------

            record_stage_changed(
                lead=lead,
                actor=user,
                pipeline=pipeline,
                old_stage=old_stage,
                new_stage=stage,
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
    #
    # Keep the existing frontend stage-movement event.
    # This must NOT be replaced by the Activity event.
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
