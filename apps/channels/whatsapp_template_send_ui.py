"""Chat endpoint for sending an approved Meta WhatsApp template."""

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from services.channels.whatsapp_template_delivery import (
    WhatsAppTemplateSendError,
    queue_template_message,
)

from .models import WhatsAppTemplate


@crm_login_required
@require_POST
def whatsapp_send_template_view(request, lead_id):
    from apps.channels.tasks import send_whatsapp_message_task

    user = request.crm_user
    lead = (
        Lead.objects.filter(
            id=lead_id,
            organization=user.organization,
        )
        .select_related("pipeline", "stage", "organization")
        .first()
    )
    if not lead:
        return JsonResponse({"error": "Lead not found."}, status=404)

    template_id = (request.POST.get("template_id") or "").strip()
    if not template_id:
        return JsonResponse({"error": "template_id is required."}, status=400)

    template = (
        WhatsAppTemplate.objects.filter(
            id=template_id,
            organization=user.organization,
            status=WhatsAppTemplate.Status.APPROVED,
        )
        .select_related("account")
        .first()
    )
    if not template:
        return JsonResponse({"error": "Approved template not found."}, status=404)

    try:
        message = queue_template_message(
            template=template,
            lead=lead,
            user=user,
        )
    except WhatsAppTemplateSendError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    send_whatsapp_message_task.delay(str(message.id))
    return JsonResponse(
        {
            "id": str(message.id),
            "status": message.status,
            "transport": "template",
        },
        status=202,
    )
