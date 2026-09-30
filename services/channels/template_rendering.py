"""One message-time parameter builder for inbox and sequence templates."""
from .campaign_policy import VARIABLE, CampaignInputError, _tokens, render_message, template_fields
from .template_media import media_defaults
from .template_service import TemplateError, _button, _ordered_buttons, available_placeholders, build_meta_body, state_for


def delivery_spec(template, state=None):
    state = state if state is not None else state_for(template)
    components = state.components
    mapping = state.placeholder_mapping or {}
    if not components:
        if getattr(template, "template_format", "standard") == "carousel":
            raise CampaignInputError("This carousel has no approved component definition. Sync templates first.")
        try:
            body, mapping = build_meta_body(organization=template.organization, body=template.body)
            buttons = [_button(item) for item in _ordered_buttons(template.buttons or [])]
        except TemplateError as exc:
            raise CampaignInputError(f"Template parameters cannot be resolved. Sync and configure the template: {exc}") from exc
        components = [{"type": "BODY", "text": body}]
        if template.attachment_type != "none":
            components.insert(0, {"type": "HEADER", "format": template.attachment_type.upper()})
        if getattr(template, "footer", ""):
            components.append({"type": "FOOTER", "text": template.footer})
        if buttons:
            components.append({"type": "BUTTONS", "buttons": buttons})
    return {"components": components, "placeholder_mapping": mapping,
            "media_defaults": media_defaults(state, components)}


def validate_delivery_bindings(spec, bindings, allowed_sources):
    """Validate explicit sending defaults without borrowing Meta sample data."""
    fields = {item["key"]: item for item in template_fields(spec) if item["kind"] == "text"}
    if not isinstance(bindings, dict) or set(bindings) - fields.keys():
        raise CampaignInputError("Template sending fields changed. Review Sending setup again.")
    result = {}
    for key, binding in bindings.items():
        if not isinstance(binding, dict) or set(binding) - {"source", "default"}:
            raise CampaignInputError("Choose a CRM field or a fallback for each template parameter.")
        source, default = binding.get("source", ""), binding.get("default", "")
        if not isinstance(source, str) or not isinstance(default, str):
            raise CampaignInputError("Template parameter values must be text.")
        source, default = source.strip(), default.strip()
        if source and source not in allowed_sources:
            raise CampaignInputError("A sending parameter refers to an unavailable CRM field. Review Sending setup.")
        if len(default) > 2048:
            raise CampaignInputError(f"{fields[key]['label']} fallback exceeds 2,048 characters.")
        if not source and not default:
            raise CampaignInputError(f"Choose a CRM field or fallback for {fields[key]['label']}.")
        result[key] = {"source": source, "default": default}
    return result


def default_delivery_bindings(template, spec, allowed_sources):
    result = {}
    for field in template_fields(spec):
        if field["kind"] != "text":
            continue
        source = field["source"] or field["key"].rsplit(".", 1)[-1]
        result[field["key"]] = {"source": source if source in allowed_sources else "", "default": ""}
        if field["key"].endswith(".code"):
            for button in template.buttons or []:
                if button.get("type") == "copy_offer":
                    result[field["key"]]["default"] = button.get("coupon_code", "")
    return result


def render_for_lead(*, template, lead, user=None):
    state = state_for(template)
    spec = delivery_spec(template, state)
    components = spec["components"]
    values = dict(getattr(lead, "attributes", None) or {})
    values.update({
        "lead_name": lead.name or "", "lead_first_name": (lead.name or "").split(" ")[0],
        "phone": lead.phone or "", "email": lead.email or "",
        "lead_source": getattr(lead, "lead_source", "") or "",
        "org_name": lead.organization.name or "",
        "user_name": getattr(user, "name", "") or getattr(user, "email", "") or "",
        "pipeline_name": lead.pipeline.name if lead.pipeline_id else "",
        "stage_name": lead.stage.name if lead.stage_id else "",
    })
    allowed_sources = {item["key"] for item in available_placeholders(organization=lead.organization)}
    bindings = default_delivery_bindings(template, spec, allowed_sources)
    bindings.update(validate_delivery_bindings(spec, getattr(state, "delivery_bindings", {}) or {}, allowed_sources))
    rendered = render_message(spec, bindings, values, allowed_sources)
    # Persist the exact main message text that corresponds to sent parameters.
    # ``body`` remains the full campaign preview; inbox snapshots render header,
    # footer, buttons and cards separately and must not duplicate those parts.
    body_component = next(
        (part for part in components if str(part.get("type", "")).lower() == "body"), {},
    )
    body_parameters = next(
        (part.get("parameters", []) for part in rendered["components"] if part.get("type") == "body"), [],
    )
    body_text = str(body_component.get("text") or "")
    replacements = dict(zip(_tokens(body_text), (item["text"] for item in body_parameters)))
    rendered["body_text"] = VARIABLE.sub(lambda match: replacements[match.group(1)], body_text)
    return rendered
