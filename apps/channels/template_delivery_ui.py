"""Organization-admin setup for reusable template parameters and attachments."""

import logging

from django.contrib import messages
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.crm.decorators import crm_login_required
from services.channels.campaign_policy import CampaignInputError, fingerprint, template_fields
from services.channels.template_media import save_delivery_media
from services.channels.template_rendering import (
    default_delivery_bindings, delivery_spec, validate_delivery_bindings,
)
from services.channels.template_service import (
    TemplateError, _validate_media_file, available_placeholders, state_for,
)

from .template_models import WhatsAppTemplateMetadata
from .template_ui import _admin, _templates

logger = logging.getLogger(__name__)


def _revision(spec, state):
    return fingerprint({"spec": spec, "bindings": state.delivery_bindings or {}})


@crm_login_required
@require_http_methods(["GET", "POST"])
def template_delivery_setup(request, template_id):
    user = request.crm_user
    if not _admin(user):
        return HttpResponseForbidden("Only organization admins can configure template sending.")
    template = _templates(user).filter(pk=template_id, status="approved").exclude(meta_template_id="").first()
    if template is None:
        raise Http404
    sources = available_placeholders(organization=user.organization)
    allowed_sources = {item["key"] for item in sources}
    state = state_for(template)
    error = ""
    saved_paths = []
    can_save = False
    spec, fields, revision = {}, [], ""
    try:
        spec = delivery_spec(template, state)
        fields = template_fields(spec)
        can_save = True
        revision = _revision(spec, state)
        if request.method == "POST":
            text_keys = {item["key"] for item in fields if item["kind"] == "text"}
            media_fields = {item["key"]: item for item in fields if item["kind"] != "text"}
            submitted_keys = {key.split(":", 1)[1] for key in request.POST if key.startswith(("source:", "default:"))}
            if submitted_keys - text_keys or any(key.removeprefix("file:") not in media_fields or not key.startswith("file:") for key in request.FILES):
                raise CampaignInputError("The submitted fields do not belong to this template.")
            bindings = validate_delivery_bindings(spec, {
                key: {"source": request.POST.get(f"source:{key}", ""), "default": request.POST.get(f"default:{key}", "")}
                for key in text_keys
            }, allowed_sources)
            for key, uploaded_file in request.FILES.items():
                _validate_media_file(media_fields[key[5:]]["kind"], uploaded_file)
            with transaction.atomic():
                # Lock the approved template and its settings before accepting
                # the revision; concurrent setup changes require a fresh review.
                template = _templates(user).select_for_update(of=("self",)).filter(pk=template_id, status="approved").first()
                if template is None:
                    raise CampaignInputError("This template is no longer available or approved.")
                state = WhatsAppTemplateMetadata.objects.select_for_update().get(template=template)
                if request.POST.get("revision") != _revision(delivery_spec(template, state), state):
                    raise CampaignInputError("The template changed while you were editing. Reload Sending setup and try again.")
                for key, uploaded_file in request.FILES.items():
                    field = media_fields[key[5:]]
                    asset = save_delivery_media(template=template, field=field["key"], kind=field["kind"], uploaded_file=uploaded_file)
                    saved_paths.append(asset["path"])
                state.delivery_bindings = bindings
                state.save(update_fields=["delivery_bindings", "updated_at"])
            saved_paths.clear()
            messages.success(request, "Sending setup saved for Chats, Cadence and future broadcasts.")
            return redirect("whatsapp-template-delivery-setup", template_id=template_id)
    except (CampaignInputError, TemplateError) as exc:
        error = str(exc)
    except Exception:
        logger.warning("Could not save template sending setup for template %s", template_id)
        error = "Unable to save sending setup. Please reload and try again."

    if error:
        # The database transaction has rolled back; only new, now-unreferenced
        # uploads are removed. Existing template files remain available.
        for path in saved_paths:
            try:
                default_storage.delete(path)
            except Exception:
                logger.warning("Could not remove an unreferenced template upload for template %s", template_id)

    defaults = default_delivery_bindings(template, spec, allowed_sources) if can_save else {}
    defaults.update({key: value for key, value in (state.delivery_bindings or {}).items() if key in defaults and isinstance(value, dict)})
    rows = []
    for field in fields:
        row = dict(field)
        if field["kind"] == "text":
            value = defaults.get(field["key"], {})
            row.update(source=value.get("source", ""), fallback=value.get("default", ""))
            if request.method == "POST":
                row.update(source=request.POST.get(f"source:{field['key']}", row["source"]), fallback=request.POST.get(f"default:{field['key']}", row["fallback"]))
        else:
            asset = (state.delivery_media or {}).get(field["key"], {})
            row["filename"] = asset.get("name", "")
            row["has_media"] = bool(field.get("default"))
            row["accept"] = {"image": "image/jpeg,image/png", "video": "video/mp4,video/3gpp", "document": ".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt"}[field["kind"]]
        rows.append(row)
    return render(request, "channels/whatsapp_template_delivery_setup.html", {
        "template": template, "fields": rows, "sources": sources, "revision": revision,
        "error": error, "can_save": can_save,
    }, status=400 if error else 200)
