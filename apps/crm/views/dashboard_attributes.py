"""CRM dashboard attribute-management HTTP views."""

import json

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import AttributeDefinition, Lead
from services.crm.attribute_service import (
    create_attribute_definition,
    delete_attribute_definition,
    update_attribute_definition,
    update_lead_attribute_values,
)


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
        .filter(is_active=True, 
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
        .filter(is_active=True, 
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
        .filter(is_active=True, 
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
