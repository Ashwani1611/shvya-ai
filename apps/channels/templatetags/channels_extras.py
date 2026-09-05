from django import template

from apps.crm.models import AttributeDefinition

register = template.Library()


@register.simple_tag
def lead_attribute_rows(lead):
    if not lead:
        return []

    definitions = AttributeDefinition.objects.filter(
        organization=lead.organization,
    ).order_by("display_order", "created_at")

    values = lead.attributes or {}
    return [
        {
            "name": definition.name,
            "key": definition.key,
            "field_type": definition.field_type,
            "options": definition.options or [],
            "value": values.get(definition.key, ""),
        }
        for definition in definitions
    ]
