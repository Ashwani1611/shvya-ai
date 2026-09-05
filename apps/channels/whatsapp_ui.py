from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage


@crm_login_required
@require_GET
def whatsapp_lead_pipeline_options_view(request, lead_id):
    """Return pipeline choices for the WhatsApp lead side panel."""
    user = request.crm_user
    lead = Lead.objects.filter(
        id=lead_id,
        organization=user.organization,
    ).first()

    if not lead:
        return JsonResponse({"error": "Lead not found."}, status=404)

    pipelines = Pipeline.objects.filter(
        organization=user.organization,
        is_active=True,
    ).order_by("name")

    return JsonResponse({
        "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else "",
        "pipelines": [
            {"id": str(pipeline.id), "name": pipeline.name}
            for pipeline in pipelines
        ],
    })


@crm_login_required
@require_POST
def whatsapp_lead_quick_update_view(request, lead_id):
    """Update fields exposed by the WhatsApp side panel.

    Phone is intentionally not accepted here. A WhatsApp conversation is
    keyed to the lead phone number, so changing it from the inbox can detach
    the visible conversation from its stored message history.
    """
    user = request.crm_user
    lead = Lead.objects.filter(
        id=lead_id,
        organization=user.organization,
    ).select_related("pipeline", "stage").first()

    if not lead:
        return JsonResponse({"error": "Lead not found."}, status=404)

    update_fields = []

    if "name" in request.POST:
        name = (request.POST.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "Name is required."}, status=400)
        lead.name = name
        update_fields.append("name")

    if "email" in request.POST:
        email = (request.POST.get("email") or "").strip()
        if email:
            try:
                validate_email(email)
            except ValidationError:
                return JsonResponse(
                    {"error": "Enter a valid email address."},
                    status=400,
                )
        lead.email = email
        update_fields.append("email")

    if "pipeline" in request.POST:
        pipeline_id = (request.POST.get("pipeline") or "").strip()
        pipeline = Pipeline.objects.filter(
            id=pipeline_id,
            organization=user.organization,
            is_active=True,
        ).first()
        if not pipeline:
            return JsonResponse({"error": "Invalid pipeline."}, status=400)

        lead.pipeline = pipeline
        update_fields.append("pipeline")

        # A stage always belongs to one pipeline. When the pipeline changes,
        # move the lead to the first active stage of the selected pipeline so
        # the inbox never keeps a stage from the previous pipeline.
        first_stage = Stage.objects.filter(
            pipeline=pipeline,
            is_active=True,
        ).order_by("display_order").first()
        lead.stage = first_stage
        lead.stage_entered_at = timezone.now()
        update_fields.extend(["stage", "stage_entered_at"])

    if "stage" in request.POST and "pipeline" not in request.POST:
        stage_id = (request.POST.get("stage") or "").strip()
        stage = Stage.objects.filter(
            id=stage_id,
            pipeline=lead.pipeline,
            is_active=True,
        ).first()
        if not stage:
            return JsonResponse({"error": "Invalid stage."}, status=400)
        lead.stage = stage
        lead.stage_entered_at = timezone.now()
        update_fields.extend(["stage", "stage_entered_at"])

    if update_fields:
        lead.save(update_fields=[*dict.fromkeys(update_fields), "updated_at"])

    return JsonResponse({
        "ok": True,
        "name": lead.name,
        "email": lead.email,
        "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else "",
        "pipeline_name": lead.pipeline.name if lead.pipeline_id else "",
        "stage_id": str(lead.stage_id) if lead.stage_id else "",
        "stage_name": lead.stage.name if lead.stage_id else "",
    })


@crm_login_required
@require_POST
def whatsapp_lead_ai_toggle_view(request, lead_id):
    user = request.crm_user
    lead = Lead.objects.filter(
        id=lead_id,
        organization=user.organization,
    ).first()

    if not lead:
        return JsonResponse({"error": "Lead not found."}, status=404)

    raw = (request.POST.get("enabled") or "").strip().lower()
    if raw not in {"1", "0", "true", "false", "on", "off"}:
        return JsonResponse({"error": "Invalid enabled value."}, status=400)

    lead.ai_enabled = raw in {"1", "true", "on"}
    lead.save(update_fields=["ai_enabled", "updated_at"])
    return JsonResponse({"ok": True, "enabled": lead.ai_enabled})


@crm_login_required
@require_POST
def whatsapp_lead_attributes_save_view(request, lead_id):
    user = request.crm_user
    lead = Lead.objects.filter(
        id=lead_id,
        organization=user.organization,
    ).first()

    if not lead:
        return JsonResponse({"error": "Lead not found."}, status=404)

    definitions = AttributeDefinition.objects.filter(
        organization=user.organization,
    )
    allowed = {definition.key for definition in definitions}

    attributes = dict(lead.attributes or {})
    for key in allowed:
        field_name = f"attr_{key}"
        if field_name in request.POST:
            attributes[key] = (request.POST.get(field_name) or "").strip()

    lead.attributes = attributes
    lead.save(update_fields=["attributes", "updated_at"])
    return JsonResponse({"ok": True, "attributes": attributes})
