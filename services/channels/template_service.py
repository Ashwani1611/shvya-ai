"""
WhatsApp message template management -- create/list templates and
render their {{variable}} placeholders when actually sending.

NOTE: create_template()/update_template() only manage SHVYA's own
copy of a template. Submitting a template to Meta for approval
(the real "status: pending -> approved" flow) requires calling
Meta's Template Management API, which isn't implemented here yet --
submit_template() is a placeholder that marks status=PENDING so the
UI has something to show, but does NOT actually call Meta. Wire in
apps.channels.providers.whatsapp (or a new template-specific client
method) before relying on this for real approvals.
"""
import re

from django.core.exceptions import ValidationError

from apps.channels.models import WhatsAppTemplate

# Variables available for substitution, grounded in fields SHVYA
# actually has -- kept intentionally smaller than Meta/Kraya's full
# variable list (booked_slot, membership_expiry, etc.) since those
# don't correspond to any real field on Lead/Organization yet. Add
# more here as the corresponding model fields are added.
AVAILABLE_VARIABLES = [
    "lead_name",
    "lead_first_name",
    "phone",
    "email",
    "org_name",
    "pipeline_name",
    "stage_name",
]


class TemplateError(Exception):
    """Raised when a template can't be created or rendered."""


def create_template(*, organization, account, created_by, name, body, category=WhatsAppTemplate.Category.MARKETING, template_format=WhatsAppTemplate.Format.STANDARD, footer="", attachment_type=WhatsAppTemplate.AttachmentType.NONE, buttons=None):

    if account.organization_id != organization.id:
        raise TemplateError("Selected account does not belong to this organization.")

    template = WhatsAppTemplate(
        organization=organization,
        account=account,
        created_by=created_by,
        name=name,
        body=body,
        category=category,
        template_format=template_format,
        footer=footer,
        attachment_type=attachment_type,
        buttons=buttons or [],
        status=WhatsAppTemplate.Status.DRAFT,
    )

    try:
        template.full_clean()
    except ValidationError as exc:
        raise TemplateError(str(exc)) from exc

    template.save()

    return template


def submit_template(*, template):
    """
    Placeholder for submitting a draft template to Meta for
    approval. Real implementation needs a Graph API call to
    POST /{waba_id}/message_templates -- not built yet, see
    module docstring.
    """
    if template.status != WhatsAppTemplate.Status.DRAFT:
        raise TemplateError(
            f"Template is already {template.get_status_display()}."
        )

    template.status = WhatsAppTemplate.Status.PENDING
    template.save(update_fields=["status", "updated_at"])

    return template


_VARIABLE_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def render_template_body(*, template, lead):
    """
    Substitutes {{variable}} placeholders in a template's body
    using the given lead's data. Unknown/unavailable variables are
    left as-is rather than raising, so a rendering issue never
    blocks a send outright -- the message just goes out with the
    literal placeholder visible, which is easy to spot and fix.
    """
    values = {
        "lead_name": lead.name,
        "lead_first_name": (lead.name or "").split(" ")[0],
        "phone": lead.phone,
        "email": lead.email or "",
        "org_name": lead.organization.name,
        "pipeline_name": lead.pipeline.name if lead.pipeline_id else "",
        "stage_name": lead.stage.name if lead.stage_id else "",
    }

    def _substitute(match):
        key = match.group(1)
        return str(values.get(key, match.group(0)))

    return _VARIABLE_PATTERN.sub(_substitute, template.body)