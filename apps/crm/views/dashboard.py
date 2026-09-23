import json
import logging
from datetime import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.db import transaction

from services.crm_activity_service import (
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

from .api import get_user_pipelines
from . import filtering as _filtering_views
from . import lead_import as _lead_import_views
from . import reminders as _reminder_views
from . import dashboard_stages as _dashboard_stage_views

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

lead_stage_create = _dashboard_stage_views.lead_stage_create
lead_stage_delete = _dashboard_stage_views.lead_stage_delete
lead_stage_rename = _dashboard_stage_views.lead_stage_rename
lead_stage_ai_toggle = _dashboard_stage_views.lead_stage_ai_toggle
_stage_entry_prompt = _dashboard_stage_views._stage_entry_prompt
lead_stage_move = _dashboard_stage_views.lead_stage_move


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

# Reminder endpoints are implemented in apps.crm.views.reminders.

# ============================================================
# NEW LEAD
# ============================================================


@crm_login_required
@require_GET
def lead_create_modal(
    request,
):
    user = request.crm_user

    pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=user.organization,
            is_active=True,
        )
    )

    selected_pipeline = (
        pipelines.first()
    )

    stages = (
        Stage.objects
        .filter(
            pipeline=selected_pipeline,
            is_active=True,
        )
        .order_by(
            "display_order",
        )
        if selected_pipeline
        else Stage.objects.none()
    )

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(is_active=True, 
            organization=user.organization,
        )
        .order_by(
            "display_order",
            "created_at",
        )
    )

    return render(
        request,
        "crm/partials/lead_create_modal.html",
        {
            "pipelines": pipelines,
            "stages": stages,
            "attribute_definitions": attribute_definitions,
        },
    )


@crm_login_required
@require_POST
def lead_create_save(
    request,
):
    user = request.crm_user

    organization = user.organization

    name = request.POST.get(
        "name",
        "",
    ).strip()

    phone = request.POST.get(
        "phone",
        "",
    ).strip()

    email = request.POST.get(
        "email",
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

    # --------------------------------------------------------
    # REQUIRED FIELDS
    # --------------------------------------------------------

    if not name:

        return HttpResponse(
            "Name is required.",
            status=400,
        )

    if not phone:

        return HttpResponse(
            "Phone number is required.",
            status=400,
        )

    if not pipeline_id:

        return HttpResponse(
            "Pipeline is required.",
            status=400,
        )

    if not stage_id:

        return HttpResponse(
            "Stage is required.",
            status=400,
        )

    # --------------------------------------------------------
    # PIPELINE
    #
    # Reuse the existing CRM permission logic.
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

    pipeline = get_object_or_404(
        allowed_pipelines,
        id=pipeline_id,
    )

    # --------------------------------------------------------
    # STAGE
    #
    # Stage must belong to the selected pipeline.
    # --------------------------------------------------------

    stage = get_object_or_404(
        Stage,
        id=stage_id,
        pipeline=pipeline,
        is_active=True,
    )

    # --------------------------------------------------------
    # CUSTOM ATTRIBUTE VALUES
    #
    # Only currently defined attributes for this
    # organization are accepted.
    # --------------------------------------------------------

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(is_active=True, 
            organization=organization,
        )
    )

    attributes = {}

    for attribute in attribute_definitions:

        field_name = (
            f"attr_{attribute.key}"
        )

        if field_name in request.POST:

            attributes[attribute.key] = (
                request.POST.get(
                    field_name,
                    "",
                ).strip()
            )

    # --------------------------------------------------------
    # CREATE LEAD
    #
    # Manual Lead creation always has source = system.
    # --------------------------------------------------------

    try:

        lead = create_lead(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
            name=name,
            phone=phone,
            email=email,
            notes=notes,
            attributes=attributes,
            lead_source="system",
        )

    except DjangoValidationError as exc:

        error_message = (
            exc.message_dict
            if hasattr(
                exc,
                "message_dict",
            )
            else exc.messages
        )

        return HttpResponse(
            f"""
            <div
                class="
                    p-4
                    text-sm
                    text-red-600
                "
            >
                Validation error: {error_message}
            </div>
            """,
            status=400,
        )

    # --------------------------------------------------------
    # SUCCESS
    #
    # The frontend will use these values to insert the
    # newly-created card into the correct stage without
    # refreshing the browser.
    # --------------------------------------------------------

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadCreated": {
                "lead_id": str(
                    lead.id
                ),
                "pipeline_id": str(
                    pipeline.id
                ),
                "stage_id": str(
                    stage.id
                ),
            }
        }
    )

    return response


# ============================================================
# IMPORT LEADS
# STEP 1 — LEAD RECENCY
# ============================================================


# Lead import endpoints are implemented in apps.crm.views.lead_import.

# Lead-table construction is owned by services.crm.dashboard_query_service
# and apps.crm.views.filtering.

@crm_login_required
@require_POST
def lead_ai_toggle(request, lead_id):
    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=request.crm_user.organization,
    )
    lead.ai_enabled = not lead.ai_enabled
    lead.save(update_fields=["ai_enabled", "updated_at"])
    return render(
        request,
        "crm/partials/lead_ai_toggle.html",
        {"lead": lead},
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

    from apps.shvya_calendar.services import attach_calendar_attachments_to_leads
    attach_calendar_attachments_to_leads(
        [lead],
        organization=organization,
    )

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
            "calendar_attachments": lead.calendar_attachments,
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
        - lead.stage_entered_at
    ).days

    lead.days_in_pipeline = (
        timezone.now()
        - lead.created_at
    ).days

    from apps.ai_engagement.services.intent_score import prepare_intent_scores
    prepare_intent_scores([lead])
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
           
    # ----------------------------------------------------
    # CUSTOM ATTRIBUTE DEFINITIONS
    #
    # Definitions belong to the Lead's organization.
    # Actual Lead values remain in lead.attributes.
    # ----------------------------------------------------

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(is_active=True, 
            organization=lead.organization,
        )
        .order_by(
            "display_order",
            "created_at",
        )
    )

    lead.attribute_definitions = (
        attribute_definitions
    )

    from apps.shvya_calendar.services import attach_calendar_attachments_to_leads
    attach_calendar_attachments_to_leads(
        [lead],
        organization=lead.organization,
    )

    lead.activities_for_card = (
    lead.activities
    .select_related(
        "actor",
        "old_pipeline",
        "new_pipeline",
        "old_stage",
        "new_stage",
    )
    .order_by(
        "-created_at",
    )
   )

    lead.has_conversation_summary = True

    return {
        "lead": lead,
        "user": user,
        "conversation_summary": True,

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

        "attribute_definitions": attribute_definitions,

        # ----------------------------------------------------
        # ENTIRE LEAD ACTIVITY
        #
        # Activity is permanently attached to the Lead.
        # It is independent of the Lead's current pipeline/stage.
        #
        # Related pipeline/stage objects are selected for efficient
        # rendering, while the historical snapshot fields remain
        # available on each LeadActivity record.
        # ----------------------------------------------------

        "activities_for_card": (
            lead.activities
            .select_related(
                "actor",
                "old_pipeline",
                "new_pipeline",
                "old_stage",
                "new_stage",
            )
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

    # --------------------------------------------------------
    # CALL NAME
    # --------------------------------------------------------

    call_name = request.POST.get(
        "call_name",
        "",
    ).strip()

    if not call_name:

        return HttpResponse(
            "Call name is required.",
            status=400,
        )

    # --------------------------------------------------------
    # CALL STATUS
    #
    # Only two statuses are allowed for the CRM Call Tracker:
    #   - completed
    #   - no_response
    # --------------------------------------------------------

    status = request.POST.get(
        "status",
        "completed",
    ).strip()

    allowed_statuses = {
        "completed",
        "no_response",
    }

    if status not in allowed_statuses:

        return HttpResponse(
            "Invalid call status.",
            status=400,
        )

    # --------------------------------------------------------
    # DURATION
    #
    # Duration applies only to completed calls.
    # --------------------------------------------------------

    duration_seconds_raw = request.POST.get(
        "duration_seconds",
        "0",
    ).strip()
    duration_seconds = int(
    duration_seconds_raw or 0
    )

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
    duration_seconds = duration_seconds * 60

    if status == "no_response":

        duration_seconds = 0

    # --------------------------------------------------------
    # CALL NOTES
    #
    # Notes apply only to completed calls.
    # --------------------------------------------------------

    notes = request.POST.get(
        "notes",
        "",
    ).strip()

    if status == "no_response":

        notes = ""

    # --------------------------------------------------------
    # CALL DATE / TIME
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # SAVE CALL
    # --------------------------------------------------------

    call = LeadCall.objects.create(
        lead=lead,
        user=user,
        call_name=call_name,
        status=status,
        duration_seconds=duration_seconds,
        notes=notes,
        called_at=called_at,
    )

    # --------------------------------------------------------
    # PERMANENT ACTIVITY
    # --------------------------------------------------------

    record_call_logged(
        lead=lead,
        actor=user,
        call=call,
    )

    # --------------------------------------------------------
    # REFRESH LEAD CARD
    # --------------------------------------------------------

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
# CONVERSATION SUMMARY
# ============================================================


@crm_login_required
@require_GET
def lead_conversation_summary_modal(
    request,
    lead_id,
):
    """
    Render the latest internal conversation summary for a Lead.

    This is a read-only UI endpoint.

    The summary is:
        InternalConversationSummary

    It is intentionally separate from:
        LeadNote / Qualification Summary
    """

    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    from apps.ai_engagement.models import (
        InternalConversationSummary,
    )

    conversation_summary = (
        InternalConversationSummary.objects
        .filter(
            organization=user.organization,
            lead=lead,
            is_active=True,
        )
        .order_by(
            "-generated_at",
        )
        .first()
    )

    return render(
        request,
        "crm/partials/lead_conversation_summary_modal.html",
        {
            "lead": lead,
            "conversation_summary": conversation_summary,
        },
    )

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

    reminder = LeadReminder.objects.create(
        lead=lead,
        assigned_to=user,
        title=title,
        description=description,
        due_at=due_at,
        status="pending",
    )

    record_reminder_created(
        lead=lead,
        actor=user,
        reminder=reminder,
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

    created_note = LeadNote.objects.create(
        lead=lead,
        created_by=user,
        note=note,
        note_type="manual",
    )

    record_note_added(
        lead=lead,
        actor=user,
        note=created_note,
    )

    lead.save(update_fields=["updated_at"])

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
@transaction.atomic
def lead_edit_save(
    request,
    lead_id,
):

    user = request.crm_user

    organization = user.organization

    lead = get_object_or_404(
        Lead.objects.select_for_update(),
        id=lead_id,
        organization=organization,
    )

    old_pipeline = lead.pipeline
    old_stage = lead.stage
    old_pipeline_id = lead.pipeline_id
    old_stage_id = lead.stage_id

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

    stage_changed = (
        old_stage_id != lead.stage_id
    )

    pipeline_changed = (
        old_pipeline_id != lead.pipeline_id
    )

    lead.name = (
        name
        or lead.name
    )

    lead.email = email

    if phone:

        lead.phone = phone

    else:

        lead.phone = ""

    # Notes are append-only LeadNote records; do not overwrite note history.

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

    if stage_changed:
        from apps.crm.services.stage_requirements import clean_entry_values, missing_attributes
        attributes, errors = clean_entry_values(lead.stage, attributes, request.POST)
        if errors or missing_attributes(lead.stage, attributes):
            return _stage_entry_prompt(request, lead, lead.stage, errors)
    lead.attributes = attributes

    try:

        if stage_changed:

            lead.stage_entered_at = timezone.now()

        lead.full_clean()

        lead.save()

        if pipeline_changed:

            record_pipeline_changed(
                lead=lead,
                actor=user,
                old_pipeline=old_pipeline,
                new_pipeline=lead.pipeline,
                old_stage=old_stage,
                new_stage=lead.stage,
            )

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

        if notes and (not latest_manual_note or latest_manual_note.note.strip() != notes.strip()):
            LeadNote.objects.create(
                lead=lead,
                created_by=user,
                note=notes,
                note_type="manual",
            )

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


# Attribute-management views live in a focused module; aliases preserve
# historical imports and all URL contracts.
from . import dashboard_attributes as _dashboard_attribute_views  # noqa: E402

attribute_create_modal = _dashboard_attribute_views.attribute_create_modal
attribute_create_save = _dashboard_attribute_views.attribute_create_save
attribute_manage_modal = _dashboard_attribute_views.attribute_manage_modal
attribute_edit_modal = _dashboard_attribute_views.attribute_edit_modal
attribute_update_save = _dashboard_attribute_views.attribute_update_save
attribute_delete = _dashboard_attribute_views.attribute_delete
lead_attribute_values_modal = _dashboard_attribute_views.lead_attribute_values_modal
lead_attribute_values_save = _dashboard_attribute_views.lead_attribute_values_save


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


# Shared lead-filter UI is implemented in apps.crm.views.filtering.

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
