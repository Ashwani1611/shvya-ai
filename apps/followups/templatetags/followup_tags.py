import re

from django import template


register = template.Library()

_SAFE_ATTRIBUTE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

_BASE_PLACEHOLDERS = [
    {"token": "{{lead_name}}", "label": "Lead name"},
    {"token": "{{lead_first_name}}", "label": "First name"},
    {"token": "{{phone}}", "label": "Number"},
    {"token": "{{email}}", "label": "Email"},
    {"token": "{{lead_source}}", "label": "Lead source"},
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

    # Use configured fields, not a sample of recent leads: a newly created
    # attribute must be available before its first value is collected.
    from services.channels.template_service import available_placeholders
    attribute_keys = {
        str(item["key"]).strip()
        for item in available_placeholders(organization=organization)
        if item.get("source") == "lead_attribute"
        and _SAFE_ATTRIBUTE_KEY.fullmatch(str(item.get("key") or "").strip())
    }

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
