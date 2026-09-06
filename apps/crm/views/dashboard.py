import json
import logging
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, Max
from django.http import (
    FileResponse,
    HttpResponse,
)
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.db import transaction
from django.conf import settings

from services.crm_activity_service import (
    record_stage_changed,
    record_pipeline_changed,
    record_reminder_created,
    record_reminder_completed,
    record_note_added,
    record_call_logged,
    record_lead_updated,
)

from services.crm.lead_import_service import (
    create_import_token,
    save_import_state,
    get_import_state,
    parse_uploaded_file,
    delete_import_state,
    normalize_import_phone,
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

from apps.crm.models.lead import (
    normalize_phone,
)

from services.crm.attribute_service import (
    create_attribute_definition,
    update_attribute_definition,
    delete_attribute_definition,
    update_lead_attribute_values,
)

from services.crm.lead_service import (
    create_lead,
)

from .api import STAGE_THEMES, get_user_pipelines
from .bulk import bulk_permissions


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
# GLOBAL REMINDERS
# ============================================================


@crm_login_required
@require_GET
def global_reminders_modal(
    request,
):
    user = request.crm_user

    reminders = (
        LeadReminder.objects
        .filter(
            lead__organization=user.organization,
            status="pending",
        )
        .select_related(
            "lead",
            "lead__pipeline",
            "lead__stage",
        )
        .order_by(
            "due_at",
        )
    )

    now = timezone.now()

    overdue_reminders = []
    today_reminders = []
    upcoming_reminders = []

    for reminder in reminders:

        local_due_at = timezone.localtime(
            reminder.due_at
        )

        if local_due_at < timezone.localtime(now):

            overdue_reminders.append(
                reminder
            )

        elif local_due_at.date() == timezone.localtime(
            now
        ).date():

            today_reminders.append(
                reminder
            )

        else:

            upcoming_reminders.append(
                reminder
            )

    return render(
        request,
        "crm/partials/global_reminders_modal.html",
        {
            "overdue_reminders": overdue_reminders,
            "today_reminders": today_reminders,
            "upcoming_reminders": upcoming_reminders,
            "overdue_count": len(
                overdue_reminders
            ),
            "today_count": len(
                today_reminders
            ),
            "upcoming_count": len(
                upcoming_reminders
            ),
            "total_count": (
                len(overdue_reminders)
                + len(today_reminders)
                + len(upcoming_reminders)
            ),
        },
    )

@crm_login_required
@require_POST
def global_reminder_complete(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )

    reminder.status = "completed"
    reminder.completed_at = timezone.now()

    reminder.save(
        update_fields=[
            "status",
            "completed_at",
            "updated_at",
        ]
    )

    record_reminder_completed(
        lead=reminder.lead,
        actor=user,
        reminder=reminder,
    )

    return HttpResponse(
        status=204
    )

@crm_login_required
@require_POST
def global_reminder_snooze(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )

    reminder.due_at = (
        reminder.due_at
        + timedelta(
            minutes=30
        )
    )

    reminder.save(
        update_fields=[
            "due_at",
            "updated_at",
        ]
    )

    return HttpResponse(
        status=204
    )

@crm_login_required
@require_POST
def global_reminder_delete(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
    )

    reminder.delete()

    return HttpResponse(
        status=204
    )

@crm_login_required
@require_GET
def global_reminder_edit_modal(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )

    return render(
        request,
        "crm/partials/global_reminder_edit_modal.html",
        {
            "reminder": reminder,
        },
    )

@crm_login_required
@require_POST
def global_reminder_edit_save(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )

    due_at_raw = request.POST.get(
        "due_at",
        "",
    ).strip()

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

            due_at = timezone.make_aware(
                due_at,
                timezone.get_current_timezone(),
            )

    except ValueError:

        return HttpResponse(
            "Invalid reminder date/time.",
            status=400,
        )

    reminder.due_at = due_at

    reminder.save(
        update_fields=[
            "due_at",
            "updated_at",
        ]
    )

    return HttpResponse(
        status=204
    )                   

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
        .filter(
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
        .filter(
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


@crm_login_required
@require_GET
def lead_import_start_modal(
    request,
):
    return render(
        request,
        "crm/partials/lead_import_start_modal.html",
        {
            "default_recency": "recent",
        },
    )


@crm_login_required
@require_POST
def lead_import_start(
    request,
):
    recency = request.POST.get(
        "recency",
        "recent",
    ).strip()

    if recency not in {
        "recent",
        "older",
    }:
        return HttpResponse(
            "Invalid lead recency selection.",
            status=400,
        )

    import_token = create_import_token()

    save_import_state(
        import_token,
        {
            "step": 1,

            "organization_id": str(
                request.crm_user.organization_id
            ),
            "user_id": str(
                request.crm_user.id
            ),

            "recency": recency,
            "filename": "",
            "extension": "",
            "headers": [],
            "rows": [],
            "row_count": 0,
            "mapping": {},
            "pipeline_id": None,
            "stage_id": None,
            "import_mode": "new_only",
        },
    )

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadImportUpload": {
                "import_token": import_token,
            }
        }
    )

    return response

# ============================================================
# IMPORT LEADS
# STEP 2 — FILE UPLOAD
# ============================================================


@crm_login_required
@require_GET
def lead_import_upload_modal(
    request,
):
    import_token = request.GET.get(
        "token",
        "",
    ).strip()

    if not import_token:

        return HttpResponse(
            "Import session is missing.",
            status=400,
        )

    state = get_import_state(
        import_token
    )

    if not state:

        return HttpResponse(
            "Import session has expired. Please start again.",
            status=400,
        )

    if str(
        state.get("organization_id", "")
    ) != str(
        request.crm_user.organization_id
    ):

        return HttpResponse(
            "Invalid import session.",
            status=403,
        )

    return render(
        request,
        "crm/partials/lead_import_upload_modal.html",
        {
            "import_token": import_token,
            "filename": state.get(
                "filename",
                "",
            ),
        },
    )


@crm_login_required
@require_POST
def lead_import_upload(
    request,
):
    import_token = request.POST.get(
        "import_token",
        "",
    ).strip()

    if not import_token:

        return HttpResponse(
            "Import session is missing.",
            status=400,
        )

    state = get_import_state(
        import_token
    )

    if not state:

        return HttpResponse(
            "Import session has expired. Please start again.",
            status=400,
        )

    if str(
        state.get("organization_id", "")
    ) != str(
        request.crm_user.organization_id
    ):

        return HttpResponse(
            "Invalid import session.",
            status=403,
        )

    uploaded_file = (
        request.FILES.get(
            "file"
        )
    )

    if uploaded_file is None:

        return HttpResponse(
            """
            <div class="p-4 text-sm text-red-600">
                Please select a file.
            </div>
            """,
            status=400,
        )

    try:

        parsed = parse_uploaded_file(
            uploaded_file
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

    state.update(
        {
            "step": 2,
            "filename": parsed["filename"],
            "extension": parsed["extension"],
            "headers": parsed["headers"],
            "rows": parsed["rows"],
            "row_count": parsed["row_count"],
        }
    )

    save_import_state(
        import_token,
        state,
    )

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadImportMapping": {
                "import_token": import_token,
            }
        }
    )

    return response

# ============================================================
# IMPORT LEADS
# STEP 3 — FIELD MAPPING
# ============================================================


@crm_login_required
@require_GET
def lead_import_mapping_modal(
    request,
):
    user = request.crm_user

    import_token = request.GET.get(
        "token",
        "",
    ).strip()

    if not import_token:

        return HttpResponse(
            "Import session is missing.",
            status=400,
        )

    state = get_import_state(
        import_token
    )

    if not state:

        return HttpResponse(
            "Import session has expired. Please start again.",
            status=400,
        )

    if str(
        state.get("organization_id", "")
    ) != str(
        user.organization_id
    ):

        return HttpResponse(
            "Invalid import session.",
            status=403,
        )

    headers = (
        state.get(
            "headers",
            [],
        )
    )

    if not headers:

        return HttpResponse(
            "No spreadsheet columns were found.",
            status=400,
        )

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
        .order_by(
            "display_order",
            "created_at",
        )
    )

    return render(
        request,
        "crm/partials/lead_import_mapping_modal.html",
        {
            "import_token": import_token,
            "headers": headers,
            "attribute_definitions": attribute_definitions,
            "mapping": state.get(
                "mapping",
                {},
            ),
        },
    )


@crm_login_required
@require_POST
def lead_import_mapping_save(
    request,
):
    user = request.crm_user

    import_token = request.POST.get(
        "import_token",
        "",
    ).strip()

    if not import_token:

        return HttpResponse(
            "Import session is missing.",
            status=400,
        )

    state = get_import_state(
        import_token
    )

    if not state:

        return HttpResponse(
            "Import session has expired. Please start again.",
            status=400,
        )

    if str(
        state.get("organization_id", "")
    ) != str(
        user.organization_id
    ):

        return HttpResponse(
            "Invalid import session.",
            status=403,
        )

    headers = state.get(
        "headers",
        [],
    )

    if not headers:

        return HttpResponse(
            "No spreadsheet columns are available.",
            status=400,
        )

    # --------------------------------------------------------
    # READ MAPPING
    #
    # Every mapping entry is:
    #
    #     SHVYA field key -> Sheet Column
    #
    # --------------------------------------------------------

    mapping = {}

    for key in request.POST:

        if not key.startswith(
            "mapping_"
        ):

            continue

        shvya_field = key[
            len("mapping_"):
        ]

        sheet_column = (
            request.POST.get(
                key,
                "",
            ).strip()
        )

        if not sheet_column:

            continue

        mapping[
            shvya_field
        ] = sheet_column

    # --------------------------------------------------------
    # VALIDATE SHEET COLUMNS
    # --------------------------------------------------------

    invalid_columns = [
        column
        for column in mapping.values()
        if column not in headers
    ]

    if invalid_columns:

        return HttpResponse(
            "One or more selected sheet columns are invalid.",
            status=400,
        )

    # --------------------------------------------------------
    # PREVENT DUPLICATE SHEET COLUMN MAPPING
    # --------------------------------------------------------

    used_columns = set()

    for shvya_field, sheet_column in mapping.items():

        if sheet_column in used_columns:

            return HttpResponse(
                f"""
                <div class="p-4 text-sm text-red-600">
                    Sheet column "{sheet_column}" cannot be mapped
                    to more than one SHVYA field.
                </div>
                """,
                status=400,
            )

        used_columns.add(
            sheet_column
        )

    # --------------------------------------------------------
    # REQUIRED FIELDS
    # --------------------------------------------------------

    name_column = mapping.get(
        "name",
        "",
    )

    phone_column = mapping.get(
        "phone",
        "",
    )

    if not name_column:

        return HttpResponse(
            """
            <div class="p-4 text-sm text-red-600">
                Name must be mapped to a sheet column.
            </div>
            """,
            status=400,
        )

    if not phone_column:

        return HttpResponse(
            """
            <div class="p-4 text-sm text-red-600">
                Phone must be mapped to a sheet column.
            </div>
            """,
            status=400,
        )

    # --------------------------------------------------------
    # SAVE MAPPING
    # --------------------------------------------------------

    state["step"] = 3

    state["mapping"] = mapping

    save_import_state(
        import_token,
        state,
    )

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadImportDestination": {
                "import_token": import_token,
            }
        }
    )

    return response

    
# ============================================================
# IMPORT LEADS
# STEP 4 — DESTINATION / ASSIGNMENT
# ============================================================


@crm_login_required
@require_GET
def lead_import_destination_modal(
    request,
):
    user = request.crm_user

    import_token = request.GET.get(
        "token",
        "",
    ).strip()

    if not import_token:

        return HttpResponse(
            "Import session is missing.",
            status=400,
        )

    state = get_import_state(
        import_token
    )

    if not state:

        return HttpResponse(
            "Import session has expired. Please start again.",
            status=400,
        )

    if str(
        state.get("organization_id", "")
    ) != str(
        user.organization_id
    ):

        return HttpResponse(
            "Invalid import session.",
            status=403,
        )

    mapping = state.get(
        "mapping",
        {},
    )

    headers = state.get(
        "headers",
        [],
    )

    if not mapping.get("name"):

        return HttpResponse(
            "Name must be mapped before continuing.",
            status=400,
        )

    if not mapping.get("phone"):

        return HttpResponse(
            "Phone must be mapped before continuing.",
            status=400,
        )

    pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=user.organization,
            is_active=True,
        )
    )

    selected_pipeline = None

    saved_pipeline_id = (
        state.get(
            "pipeline_id"
        )
    )

    if saved_pipeline_id:

        selected_pipeline = (
            pipelines
            .filter(
                id=saved_pipeline_id,
            )
            .first()
        )

    if selected_pipeline is None:

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

    selected_stage = None

    saved_stage_id = (
        state.get(
            "stage_id"
        )
    )

    if saved_stage_id:

        selected_stage = (
            stages
            .filter(
                id=saved_stage_id,
            )
            .first()
        )

    if selected_stage is None:

        selected_stage = (
            stages.first()
        )

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
        .order_by(
            "display_order",
            "created_at",
        )
    )

    # Build a display mapping list so the UI can show
    # SHVYA Field -> Sheet Column.
    mapping_rows = []

    field_labels = {
        "name": "Name",
        "phone": "Phone",
        "email": "Email",
    }

    for field_key, field_label in field_labels.items():

        mapping_rows.append(
            {
                "key": field_key,
                "label": field_label,
                "sheet_column": mapping.get(
                    field_key,
                    "",
                ),
            }
        )

    for attribute in attribute_definitions:

        sheet_column = mapping.get(
            attribute.key,
            "",
        )

        if not sheet_column:

            continue

        mapping_rows.append(
            {
                "key": attribute.key,
                "label": attribute.name,
                "sheet_column": sheet_column,
            }
        )

    return render(
        request,
        "crm/partials/lead_import_destination_modal.html",
        {
            "import_token": import_token,
            "headers": headers,
            "mapping_rows": mapping_rows,
            "pipelines": pipelines,
            "stages": stages,
            "selected_pipeline_id": (
                str(
                    selected_pipeline.id
                )
                if selected_pipeline
                else ""
            ),
            "selected_stage_id": (
                str(
                    selected_stage.id
                )
                if selected_stage
                else ""
            ),
        },
    )


@crm_login_required
@require_POST
def lead_import_destination_save(
    request,
):
    user = request.crm_user

    import_token = request.POST.get(
        "import_token",
        "",
    ).strip()

    if not import_token:
        return HttpResponse(
            "Import session is missing.",
            status=400,
        )

    state = get_import_state(
        import_token
    )

    if not state:
        return HttpResponse(
            "Import session has expired. Please start again.",
            status=400,
        )

    if str(
        state.get("organization_id", "")
    ) != str(
        user.organization_id
    ):
        return HttpResponse(
            "Invalid import session.",
            status=403,
        )

    pipeline_id = request.POST.get(
        "pipeline",
        "",
    ).strip()

    stage_id = request.POST.get(
        "stage",
        "",
    ).strip()

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

    allowed_pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=user.organization,
            is_active=True,
        )
    )

    pipeline = (
        allowed_pipelines
        .filter(
            id=pipeline_id,
        )
        .first()
    )

    if not pipeline:
        return HttpResponse(
            "Invalid destination pipeline.",
            status=400,
        )

    stage = (
        Stage.objects
        .filter(
            id=stage_id,
            pipeline=pipeline,
            is_active=True,
        )
        .first()
    )

    if not stage:
        return HttpResponse(
            "Invalid destination stage.",
            status=400,
        )

    # --------------------------------------------------------
    # SAVE DESTINATION
    # --------------------------------------------------------

    state["step"] = 4

    state["pipeline_id"] = str(
        pipeline.id
    )

    state["stage_id"] = str(
        stage.id
    )

    save_import_state(
        import_token,
        state,
    )

    # --------------------------------------------------------
    # BUILD REVIEW DATA
    # --------------------------------------------------------

    mapping = state.get(
        "mapping",
        {},
    )

    rows = state.get(
        "rows",
        [],
    )

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
    )

    new_lead_count = 0
    existing_lead_count = 0
    invalid_phone_count = 0

    preview_rows = []

    name_column = mapping.get(
        "name"
    )

    phone_column = mapping.get(
        "phone"
    )

    email_column = mapping.get(
        "email"
    )

    for index, row in enumerate(
        rows,
        start=1,
    ):
        name = (
            row.get(
                name_column,
                "",
            )
            or ""
        ).strip()

        raw_phone = (
            row.get(
                phone_column,
                "",
            )
            or ""
        ).strip()

        email = (
            row.get(
                email_column,
                "",
            )
            or ""
        ).strip()

        try:

            normalized_phone = (
                normalize_import_phone(
                    raw_phone
                )
            )

            normalized_phone = (
                normalize_phone(
                    normalized_phone
                )
            )

        except DjangoValidationError:

            invalid_phone_count += 1

            preview_rows.append(
                {
                    "row_number": index,
                    "name": name,
                    "phone": raw_phone,
                    "normalized_phone": "",
                    "email": email,
                    "status": "invalid",
                }
            )

            continue

        existing_lead = (
            Lead.objects
            .filter(
                organization=user.organization,
                phone=normalized_phone,
            )
            .first()
        )

        if existing_lead:

            existing_lead_count += 1
            status = "existing"

        else:

            new_lead_count += 1
            status = "new"

        preview_rows.append(
            {
                "row_number": index,
                "name": name,
                "phone": raw_phone,
                "normalized_phone": normalized_phone,
                "email": email,
                "status": status,
            }
        )

    state["review"] = {
        "new_lead_count": new_lead_count,
        "existing_lead_count": existing_lead_count,
        "invalid_phone_count": invalid_phone_count,
    }

    save_import_state(
        import_token,
        state,
    )

    # --------------------------------------------------------
    # DIRECTLY RETURN STEP 5
    #
    # No HX-Trigger.
    # No second JavaScript request.
    # --------------------------------------------------------

    return render(
        request,
        "crm/partials/lead_import_review_modal.html",
        {
            "import_token": import_token,
            "filename": state.get(
                "filename",
                "",
            ),
            "row_count": state.get(
                "row_count",
                0,
            ),
            "new_lead_count": new_lead_count,
            "existing_lead_count": existing_lead_count,
            "invalid_phone_count": invalid_phone_count,
            "pipeline": pipeline,
            "stage": stage,
            "preview_rows": preview_rows,
        },
    )

# ============================================================
# IMPORT LEADS
# STEP 5 — REVIEW & IMPORT
# ============================================================


@crm_login_required
@require_GET
def lead_import_review_modal(
    request,
):
    user = request.crm_user

    import_token = request.GET.get(
        "token",
        "",
    ).strip()

    if not import_token:

        return HttpResponse(
            "Import session is missing.",
            status=400,
        )

    state = get_import_state(
        import_token
    )

    if not state:

        return HttpResponse(
            "Import session has expired. Please start again.",
            status=400,
        )

    if str(
        state.get("organization_id", "")
    ) != str(
        user.organization_id
    ):

        return HttpResponse(
            "Invalid import session.",
            status=403,
        )

    mapping = state.get(
        "mapping",
        {},
    )

    rows = state.get(
        "rows",
        [],
    )

    if not mapping.get("name"):

        return HttpResponse(
            "Name must be mapped before continuing.",
            status=400,
        )

    if not mapping.get("phone"):

        return HttpResponse(
            "Phone must be mapped before continuing.",
            status=400,
        )

    pipeline_id = state.get(
        "pipeline_id"
    )

    stage_id = state.get(
        "stage_id"
    )

    if not pipeline_id or not stage_id:

        return HttpResponse(
            "Pipeline and stage must be selected before continuing.",
            status=400,
        )

    pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=user.organization,
            is_active=True,
        )
    )

    pipeline = (
        pipelines
        .filter(
            id=pipeline_id,
        )
        .first()
    )

    if not pipeline:

        return HttpResponse(
            "Invalid destination pipeline.",
            status=400,
        )

    stage = (
        Stage.objects
        .filter(
            id=stage_id,
            pipeline=pipeline,
            is_active=True,
        )
        .first()
    )

    if not stage:

        return HttpResponse(
            "Invalid destination stage.",
            status=400,
        )

    # --------------------------------------------------------
    # EXISTING ATTRIBUTE DEFINITIONS
    # --------------------------------------------------------

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
    )

    attribute_by_key = {
        attribute.key: attribute
        for attribute in attribute_definitions
    }

    # --------------------------------------------------------
    # CHECK EACH ROW
    # --------------------------------------------------------

    new_lead_count = 0
    existing_lead_count = 0

    invalid_phone_count = 0

    preview_rows = []

    for index, row in enumerate(
        rows,
        start=1,
    ):

        name_column = mapping.get(
            "name"
        )

        phone_column = mapping.get(
            "phone"
        )

        email_column = mapping.get(
            "email"
        )

        name = (
            row.get(
                name_column,
                "",
            )
            or ""
        ).strip()

        raw_phone = (
            row.get(
                phone_column,
                "",
            )
            or ""
        ).strip()

        email = (
            row.get(
                email_column,
                "",
            )
            or ""
        ).strip()

        normalized_phone = ""

        if raw_phone:

            try:

                normalized_phone = (
                    normalize_import_phone(
                        raw_phone
                    )
                )

                normalized_phone = normalize_phone(
                    normalized_phone
                )

            except DjangoValidationError:

                invalid_phone_count += 1

                preview_rows.append(
                    {

                        "row_number": index,
                        "name": name,
                        "phone": raw_phone,
                        "normalized_phone": "",
                        "email": email,
                        "status": "invalid",
                    }
                )  

                continue

        existing_lead = None

        if normalized_phone:

            existing_lead = (
                Lead.objects
                .filter(
                    organization=user.organization,
                    phone=normalized_phone,
                )
                .first()
            )

        if existing_lead:

            existing_lead_count += 1
            status = "existing"

        else:

            new_lead_count += 1
            status = "new"

        preview_rows.append(
            {
                "row_number": index,
                "name": name,
                "phone": raw_phone,
                "normalized_phone": normalized_phone,
                "email": email,
                "status": status,
            }
        )

    state["review"] = {
        "new_lead_count": new_lead_count,
        "existing_lead_count": existing_lead_count,
        "invalid_phone_count": invalid_phone_count,
    }

    save_import_state(
        import_token,
        state,
    )

    return render(
        request,
        "crm/partials/lead_import_review_modal.html",
        {
            "import_token": import_token,
            "filename": state.get(
                "filename",
                "",
            ),
            "row_count": state.get(
                "row_count",
                0,
            ),
            "new_lead_count": new_lead_count,
            "existing_lead_count": existing_lead_count,
            "invalid_phone_count": invalid_phone_count,
            "pipeline": pipeline,
            "stage": stage,
            "preview_rows": preview_rows,
        },
    )


@crm_login_required
@require_POST
def lead_import_execute(
    request,
):
    user = request.crm_user

    import_token = request.POST.get(
        "import_token",
        "",
    ).strip()

    if not import_token:

        return HttpResponse(
            "Import session is missing.",
            status=400,
        )

    state = get_import_state(
        import_token
    )

    if not state:

        return HttpResponse(
            "Import session has expired. Please start again.",
            status=400,
        )

    if str(
        state.get("organization_id", "")
    ) != str(
        user.organization_id
    ):

        return HttpResponse(
            "Invalid import session.",
            status=403,
        )

    import_mode = request.POST.get(
        "import_mode",
        "new_only",
    ).strip()

    if import_mode not in {
        "new_only",
        "new_and_existing",
    }:

        return HttpResponse(
            "Invalid import mode.",
            status=400,
        )

    mapping = state.get(
        "mapping",
        {},
    )

    rows = state.get(
        "rows",
        [],
    )

    pipeline_id = state.get(
        "pipeline_id"
    )

    stage_id = state.get(
        "stage_id"
    )

    if not mapping.get("name"):

        return HttpResponse(
            "Name mapping is missing.",
            status=400,
        )

    if not mapping.get("phone"):

        return HttpResponse(
            "Phone mapping is missing.",
            status=400,
        )

    if not pipeline_id or not stage_id:

        return HttpResponse(
            "Pipeline and stage are required.",
            status=400,
        )

    allowed_pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=user.organization,
            is_active=True,
        )
    )

    pipeline = get_object_or_404(
        allowed_pipelines,
        id=pipeline_id,
    )

    stage = get_object_or_404(
        Stage,
        id=stage_id,
        pipeline=pipeline,
        is_active=True,
    )

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
    )

    created_count = 0
    updated_count = 0
    skipped_count = 0
    invalid_count = 0

    name_column = mapping.get(
        "name"
    )

    phone_column = mapping.get(
        "phone"
    )

    email_column = mapping.get(
        "email"
    )

    # --------------------------------------------------------
    # IMPORT EACH ROW
    # --------------------------------------------------------

    for row in rows:

        name = (
            row.get(
                name_column,
                "",
            )
            or ""
        ).strip()

        raw_phone = (
            row.get(
                phone_column,
                "",
            )
            or ""
        ).strip()

        email = (
            row.get(
                email_column,
                "",
            )
            or ""
        ).strip()

        if not name or not raw_phone:

            invalid_count += 1

            continue

        try:

            normalized_phone = (
                normalize_import_phone(
                    raw_phone
                )
            )

            normalized_phone = (
                normalize_phone(
                    normalized_phone
                )
            )

        except DjangoValidationError:

            invalid_count += 1

            continue

        existing_lead = (
            Lead.objects
            .filter(
                organization=user.organization,
                phone=normalized_phone,
            )
            .first()
        )

        # ----------------------------------------------------
        # BUILD ATTRIBUTE VALUES
        # ----------------------------------------------------

        attributes = {}

        for attribute in attribute_definitions:

            sheet_column = mapping.get(
                attribute.key
            )

            if not sheet_column:

                continue

            value = (
                row.get(
                    sheet_column,
                    "",
                )
                or ""
            ).strip()

            if value:

                attributes[
                    attribute.key
                ] = value

        # ----------------------------------------------------
        # EXISTING LEAD
        # ----------------------------------------------------

        if existing_lead:

            # ------------------------------------------------
            # NEW LEADS ONLY
            #
            # Existing leads are completely untouched.
            # ------------------------------------------------

            if import_mode == "new_only":

                skipped_count += 1

                continue

            # ------------------------------------------------
            # NEW & EXISTING LEADS
            #
            # Move the existing lead to the selected
            # pipeline and stage.
            # ------------------------------------------------

            existing_lead.pipeline = pipeline

            existing_lead.stage = stage

            existing_lead.name = (
                name
                or existing_lead.name
            )

            if email:

                existing_lead.email = email

            if attributes:

                existing_lead.attributes = {
                    **(
                        existing_lead.attributes
                        or {}
                    ),
                    **attributes,
                }

            existing_lead.full_clean()

            existing_lead.save()

            updated_count += 1

            continue

        # ----------------------------------------------------
        # NEW LEAD
        # ----------------------------------------------------

        lead = create_lead(
            organization=user.organization,
            pipeline=pipeline,
            stage=stage,
            name=name,
            phone=normalized_phone,
            email=email,
            attributes=attributes,
            lead_source="csv_import",
        )

        created_count += 1

    # --------------------------------------------------------
    # CLEAN UP TEMPORARY STATE
    # --------------------------------------------------------

    delete_import_state(
        import_token
    )

    return render(
        request,
        "crm/partials/lead_import_progress_modal.html",
        {
            "created_count": created_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "invalid_count": invalid_count,
        },
    )

@crm_login_required
@require_GET
def lead_import_sample_file(
    request,
):
    sample_path = (
        settings.BASE_DIR
        / "static"
        / "crm"
        / "import"
        / "Contacts_Upload_Sample.xlsx"
    )

    if not sample_path.exists():

        return HttpResponse(
            "Sample file is currently unavailable.",
            status=404,
        )

    return FileResponse(
        sample_path.open(
            "rb"
        ),
        as_attachment=True,
        filename="Contacts_Upload_Sample.xlsx",
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )

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
            get_user_pipelines(user)
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
                - lead.stage_entered_at
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

            # ----------------------------------------------------
            # ENTIRE LEAD ACTIVITY
            #
            # Activity belongs permanently to the Lead.
            # It is independent of the Lead's current pipeline
            # and stage.
            # ----------------------------------------------------

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

            lead.attribute_definitions = (
                AttributeDefinition.objects
                .filter(
                    organization=lead.organization,
                )
                .order_by(
                    "display_order",
                    "created_at",
                )
            )

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
        "bulk_permissions": bulk_permissions(user, pipeline),
        "bulk_query": request.GET.urlencode(),

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
        - lead.stage_entered_at
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
           
    # ----------------------------------------------------
    # CUSTOM ATTRIBUTE DEFINITIONS
    #
    # Definitions belong to the Lead's organization.
    # Actual Lead values remain in lead.attributes.
    # ----------------------------------------------------

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(
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
# ATTRIBUTE MANAGEMENT
# ============================================================


@crm_login_required
@require_GET
def attribute_create_modal(
    request,
):
    user = request.crm_user

    attributes_count = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
        .count()
    )

    lead_id = request.GET.get(
        "lead_id",
        "",
    ).strip()

    import_token = request.GET.get(
        "import_token",
        "",
    ).strip()

    return render(
        request,
        "crm/partials/attribute_create_modal.html",
        {
            "attribute_types": (
                AttributeDefinition.FieldType.choices
            ),
            "attribute_count": attributes_count,
            "max_attributes": 15,
            "lead_id": lead_id,
            "import_token": import_token,
        },
    )


@crm_login_required
@require_POST
def attribute_create_save(
    request,
):
    user = request.crm_user

    name = request.POST.get(
        "name",
        "",
    ).strip()

    field_type = request.POST.get(
        "field_type",
        "",
    ).strip()

    description = request.POST.get(
        "description",
        "",
    ).strip()

    options = [
        value.strip()
        for value in request.POST.getlist(
            "options",
        )
        if value.strip()
    ]

    lead_id = request.POST.get(
        "lead_id",
        "",
    ).strip()

    import_token = request.POST.get(
        "import_token",
        "",
    ).strip()

    try:

        attribute = create_attribute_definition(
            organization=user.organization,
            name=name,
            field_type=field_type,
            description=description,
            options=options,
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

    response = HttpResponse("")

    # --------------------------------------------------------
    # IMPORT MAPPING FLOW
    # --------------------------------------------------------

    if import_token:

        response["HX-Trigger"] = json.dumps(
            {
                "attributeCreatedForImport": {
                    "attribute_id": str(
                        attribute.id
                    ),
                    "import_token": import_token,
                }
            }
        )

        return response

    # --------------------------------------------------------
    # NORMAL ATTRIBUTE FLOW
    # --------------------------------------------------------

    response["HX-Trigger"] = json.dumps(
        {
            "attributeCreated": {
                "attribute_id": str(
                    attribute.id
                ),
                "lead_id": lead_id,
            }
        }
    )

    return response


@crm_login_required
@require_GET
def attribute_manage_modal(
    request,
):
    user = request.crm_user

    attributes = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
        .order_by(
            "display_order",
            "created_at",
        )
    )

    lead_id = request.GET.get(
        "lead_id",
        "",
    ).strip()

    return render(
        request,
        "crm/partials/manage_attributes_modal.html",
        {
            "attributes": attributes,
            "attribute_count": attributes.count(),
            "max_attributes": 15,
            "lead_id": lead_id,
        },
    )

@crm_login_required
@require_GET
def attribute_edit_modal(
    request,
    attribute_id,
):
    user = request.crm_user

    attribute = get_object_or_404(
        AttributeDefinition,
        id=attribute_id,
        organization=user.organization,
    )

    lead_id = request.GET.get(
        "lead_id",
        "",
    ).strip()

    return render(
        request,
        "crm/partials/attribute_edit_modal.html",
        {
            "attribute": attribute,
            "attribute_types": (
                AttributeDefinition.FieldType.choices
            ),
            "lead_id": lead_id,
        },
    )


@crm_login_required
@require_POST
def attribute_update_save(
    request,
    attribute_id,
):
    user = request.crm_user

    lead_id = request.POST.get(
        "lead_id",
        "",
    ).strip()

    attribute = get_object_or_404(
        AttributeDefinition,
        id=attribute_id,
        organization=user.organization,
    )

    name = request.POST.get(
        "name",
        "",
    ).strip()

    field_type = request.POST.get(
        "field_type",
        "",
    ).strip()

    description = request.POST.get(
        "description",
        "",
    ).strip()

    options = [
        value.strip()
        for value in request.POST.getlist(
            "options",
        )
        if value.strip()
    ]

    try:

        update_attribute_definition(
            organization=user.organization,
            attribute=attribute,
            name=name,
            field_type=field_type,
            description=description,
            options=options,
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

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "attributeUpdated": {
                "attribute_id": str(
                    attribute.id
                ),
                "lead_id": lead_id,
            }
        }
    )

    return response


@crm_login_required
@require_POST
def attribute_delete(
    request,
    attribute_id,
):
    user = request.crm_user

    lead_id = request.POST.get(
        "lead_id",
        "",
    ).strip()

    attribute = get_object_or_404(
        AttributeDefinition,
        id=attribute_id,
        organization=user.organization,
    )

    try:

        delete_attribute_definition(
            organization=user.organization,
            attribute=attribute,
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

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "attributeDeleted": {
                "attribute_id": str(
                    attribute.id
                ),
                "lead_id": lead_id,
            }
        }
    )

    return response


@crm_login_required
@require_GET
def lead_attribute_values_modal(
    request,
    lead_id,
):
    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
        .order_by(
            "display_order",
            "created_at",
        )
    )

    return render(
        request,
        "crm/partials/lead_attribute_values_modal.html",
        {
            "lead": lead,
            "attribute_definitions": attribute_definitions,
        },
    )


@crm_login_required
@require_POST
def lead_attribute_values_save(
    request,
    lead_id,
):
    user = request.crm_user

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )

    attribute_definitions = (
        AttributeDefinition.objects
        .filter(
            organization=user.organization,
        )
    )

    values = {}

    for attribute in attribute_definitions:

        field_name = (
            f"attr_{attribute.key}"
        )

        if field_name in request.POST:

            values[attribute.key] = (
                request.POST.get(
                    field_name,
                    "",
                )
            )

    try:

        update_lead_attribute_values(
            organization=user.organization,
            lead=lead,
            values=values,
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

    response = HttpResponse("")

    response["HX-Trigger"] = json.dumps(
        {
            "leadAttributeValuesUpdated": {
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
