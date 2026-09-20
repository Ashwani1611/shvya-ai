"""Entry requirements for human, single-lead moves only.

Bulk and automated transition services deliberately do not call this module.
Definitions are resolved by immutable ID within the lead's organization.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from apps.crm.models import AttributeDefinition


def required_attributes(stage):
    return AttributeDefinition.objects.filter(
        organization_id=stage.pipeline.organization_id,
        id__in=(stage.config or {}).get("required_attribute_ids", []),
    )


def has_value(value):
    return (
        value is not None and value != [] and value != {} and str(value).strip() != ""
    )


def missing_attributes(stage, values):
    return [
        attribute
        for attribute in required_attributes(stage)
        if not has_value(values.get(attribute.key))
    ]


def clean_entry_values(stage, values, submitted):
    result = dict(values or {})
    errors = []
    for attribute in required_attributes(stage):
        field = f"attr_{attribute.key}"
        if field not in submitted:
            continue
        value = str(submitted[field]).strip()
        if value:
            try:
                if attribute.field_type == "numeric" and not Decimal(value).is_finite():
                    raise ValueError()
                if attribute.field_type == "date":
                    date.fromisoformat(value)
                if attribute.field_type == "datetime":
                    datetime.fromisoformat(value)
                if attribute.field_type == "option" and value not in attribute.options:
                    raise ValueError()
            except (ValueError, InvalidOperation):
                errors.append(f"Enter a valid value for {attribute.name}.")
                continue
        result[attribute.key] = value
    return result, errors
