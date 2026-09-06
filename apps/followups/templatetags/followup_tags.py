import re

from django import template

from apps.crm.models import Lead


register = template.Library()

_SAFE_ATTRIBUTE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

_BASE_PLACEHOLDERS = [
    {"token": "{{lead_name}}", "label": "Lead name"},
    {"token": "{{lead_first_name}}", "label": "First name"},
    {"token": "{{phone}}", "label": "Number"},
    {"token": "{{email}}", "label": "Email"},
    {"token": "{{user_name}}", "label": "User name"},
    {"token": "{{org_name}}", "label": "Organisation"},
    {"token": "{{pipeline_name}}", "label": "Pipeline"},
    {"token": "{{stage_name}}", "label": "Stage"},
]


@register.simple_tag
def followup_placeholders(organization):
    """Return core CRM placeholders plus real attribute keys used by the org."""
    placeholders = list(_BASE_PLACEHOLDERS)
    if not organization:
        return placeholders

    attribute_keys = set()
    attribute_rows = (
        Lead.objects.filter(organization=organization)
        .order_by("-updated_at")
        .values_list("attributes", flat=True)[:500]
    )
    for attributes in attribute_rows:
        if not isinstance(attributes, dict):
            continue
        for key in attributes:
            key = str(key).strip()
            if _SAFE_ATTRIBUTE_KEY.match(key):
                attribute_keys.add(key)

    reserved_tokens = {item["token"] for item in placeholders}
    for key in sorted(attribute_keys, key=str.lower):
        token = "{{" + key + "}}"
        if token in reserved_tokens:
            continue
        placeholders.append(
            {
                "token": token,
                "label": key.replace("_", " ").replace("-", " ").title(),
                "is_attribute": True,
            }
        )
    return placeholders
