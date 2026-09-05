from django.http import JsonResponse
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import AttributeDefinition, Lead, Stage


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

    if "stage" in request.POST:
        stage_id = (request.POST.get("stage") or "").strip()
        stage = Stage.objects.filter(
            id=stage_id,
            pipeline=lead.pipeline,
            is_active=True,
        ).first()
        if not stage:
            return JsonResponse({"error": "Invalid stage."}, status=400)
        lead.stage = stage
        update_fields.extend(["stage", "stage_entered_at"])
        from django.utils import timezone
        lead.stage_entered_at = timezone.now()

    if update_fields:
        lead.save(update_fields=[*dict.fromkeys(update_fields), "updated_at"])

    return JsonResponse({
        "ok": True,
        "name": lead.name,
        "stage_id": str(lead.stage_id),
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
