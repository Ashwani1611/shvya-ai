# ruff: noqa: F401
"""Lead import HTTP workflow extracted from the CRM dashboard module."""

import json
import logging
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, Max
from django.http import (
    FileResponse,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.db import transaction
from django.conf import settings

from services.crm.reminder_notification_service import (
    acknowledge_reminder_notification,
    due_reminder_notifications,
    reset_reminder_notification_acknowledgements,
)

from services.crm_activity_service import (
    record_stage_changed,
    record_pipeline_changed,
    record_reminder_created,
    record_reminder_completed,
    record_note_added,
    record_call_logged,
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
        .filter(is_active=True, 
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
        .filter(is_active=True, 
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

        create_lead(
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


