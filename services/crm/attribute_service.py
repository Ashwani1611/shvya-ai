"""
AttributeDefinition business logic.

Organization-level custom Lead attribute management lives here,
never in views.
"""

import re

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.crm.models import AttributeDefinition, Lead


MAX_CUSTOM_ATTRIBUTES = 15


def _build_attribute_key(name):
    """
    Build a stable internal key from the initial attribute name.

    Examples:
        "Company" -> "company"
        "Project Details" -> "project_details"
        "Business Type" -> "business_type"
    """

    key = re.sub(
        r"[^a-zA-Z0-9]+",
        "_",
        name.strip().lower(),
    )

    key = re.sub(
        r"_+",
        "_",
        key,
    ).strip("_")

    if not key:
        raise ValidationError(
            {
                "name": (
                    "Attribute name must contain "
                    "letters or numbers."
                )
            }
        )

    return key


def _validate_field_type(field_type):
    """
    Validate that the requested field type is supported.
    """

    valid_field_types = {
        choice[0]
        for choice in AttributeDefinition.FieldType.choices
    }

    if field_type not in valid_field_types:

        raise ValidationError(
            {
                "field_type": "Invalid attribute type."
            }
        )


def _clean_options(
    *,
    field_type,
    options,
):
    """
    Normalize options for an attribute definition.

    Option Picker requires at least one option.
    All other field types store no options.
    """

    if field_type != AttributeDefinition.FieldType.OPTION:
        return []

    if not isinstance(
        options,
        list,
    ):
        raise ValidationError(
            {
                "options": (
                    "Options must be stored as a list."
                )
            }
        )

    cleaned_options = []

    for option in options:

        value = str(
            option
        ).strip()

        if not value:
            continue

        if value not in cleaned_options:
            cleaned_options.append(value)

    if not cleaned_options:

        raise ValidationError(
            {
                "options": (
                    "Option Picker must have at least "
                    "one option."
                )
            }
        )

    return cleaned_options


def create_attribute_definition(
    *,
    organization,
    name,
    field_type=AttributeDefinition.FieldType.TEXT,
    description="",
    options=None,
):
    """
    Create one custom attribute definition for an organization.

    The maximum number of custom attributes is 15.

    This function creates only the definition. It does not modify
    any Lead.attributes JSON values.
    """

    name = (
        name or ""
    ).strip()

    description = (
        description or ""
    ).strip()

    if not name:

        raise ValidationError(
            {
                "name": "Attribute name is required."
            }
        )

    _validate_field_type(
        field_type
    )

    options = _clean_options(
        field_type=field_type,
        options=(
            options
            if isinstance(options, list)
            else []
        ),
    )

    with transaction.atomic():

        attribute_count = (
            AttributeDefinition.objects
            .filter(
                organization=organization,
            )
            .count()
        )

        if attribute_count >= MAX_CUSTOM_ATTRIBUTES:

            raise ValidationError(
                {
                    "name": (
                        f"An organization can have a maximum "
                        f"of {MAX_CUSTOM_ATTRIBUTES} custom attributes."
                    )
                }
            )

        key = _build_attribute_key(
            name
        )

        if AttributeDefinition.objects.filter(
            organization=organization,
            name=name,
        ).exists():

            raise ValidationError(
                {
                    "name": (
                        "An attribute with this name "
                        "already exists."
                    )
                }
            )

        if AttributeDefinition.objects.filter(
            organization=organization,
            key=key,
        ).exists():

            raise ValidationError(
                {
                    "name": (
                        "An attribute with a similar name "
                        "already exists."
                    )
                }
            )

        next_display_order = (
            AttributeDefinition.objects
            .filter(
                organization=organization,
            )
            .order_by(
                "-display_order"
            )
            .values_list(
                "display_order",
                flat=True,
            )
            .first()
        )

        display_order = (
            (next_display_order + 1)
            if next_display_order is not None
            else 0
        )

        attribute = AttributeDefinition(
            organization=organization,
            name=name,
            key=key,
            field_type=field_type,
            description=description,
            options=options,
            display_order=display_order,
        )

        attribute.full_clean()
        attribute.save()

        return attribute


def update_attribute_definition(
    *,
    organization,
    attribute,
    name,
    field_type,
    description="",
    options=None,
):
    """
    Update an existing custom attribute definition.

    The internal key is deliberately preserved. Renaming the
    attribute therefore does not break existing Lead.attributes
    values.

    This function does not modify Lead attribute values.
    """

    if attribute.organization_id != organization.id:

        raise ValidationError(
            {
                "attribute": (
                    "Attribute does not belong "
                    "to this organization."
                )
            }
        )

    name = (
        name or ""
    ).strip()

    description = (
        description or ""
    ).strip()

    if not name:

        raise ValidationError(
            {
                "name": "Attribute name is required."
            }
        )

    _validate_field_type(
        field_type
    )

    options = _clean_options(
        field_type=field_type,
        options=(
            options
            if isinstance(options, list)
            else []
        ),
    )

    duplicate_name = (
        AttributeDefinition.objects
        .filter(
            organization=organization,
            name=name,
        )
        .exclude(
            id=attribute.id,
        )
        .exists()
    )

    if duplicate_name:

        raise ValidationError(
            {
                "name": (
                    "An attribute with this name "
                    "already exists."
                )
            }
        )

    with transaction.atomic():

        attribute.name = name

        attribute.field_type = field_type

        attribute.description = description

        attribute.options = options

        attribute.full_clean()

        attribute.save()

        return attribute


def delete_attribute_definition(
    *,
    organization,
    attribute,
):
    """
    Delete a custom attribute definition and remove its stored
    Lead values.

    Both operations happen in one transaction so the definition
    and its Lead values cannot become partially deleted.
    """

    if attribute.organization_id != organization.id:

        raise ValidationError(
            {
                "attribute": (
                    "Attribute does not belong "
                    "to this organization."
                )
            }
        )

    attribute_key = attribute.key

    with transaction.atomic():

        leads = (
            Lead.objects
            .select_for_update()
            .filter(
                organization=organization,
            )
        )

        for lead in leads:

            current_attributes = dict(
                lead.attributes or {}
            )

            if attribute_key not in current_attributes:
                continue

            current_attributes.pop(
                attribute_key,
                None,
            )

            lead.attributes = current_attributes

            lead.save(
                update_fields=[
                    "attributes",
                    "updated_at",
                ]
            )

        attribute.delete()