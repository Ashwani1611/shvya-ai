from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.decorators import crm_login_required
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_automation_service import hosted_queue_items


@crm_login_required
@require_GET
def hosted_session_queue_view(request, account_id):
    if not is_hosted_account_enabled(request.crm_user.organization):
        raise Http404("Hosted Account is not enabled for this organization.")

    account = get_object_or_404(
        WhatsAppAccount,
        id=account_id,
        organization=request.crm_user.organization,
        connection_type="hosted",
        is_active=True,
    )

    items = hosted_queue_items(account=account)
    queued_messages = (
        WhatsAppMessage.objects.filter(
            organization=account.organization,
            account=account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status=WhatsAppMessage.Status.QUEUED,
        )
        .select_related("lead")
        .order_by("created_at")[:200]
    )
    for message in queued_messages:
        payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        origin = (
            (payload.get("shvya_ai") and "AI Engagement")
            or (payload.get("shvya_auto_followup") and "Auto Follow-up")
            or (payload.get("shvya_hosted") or {}).get("origin")
            or "Queued message"
        )
        items.append(
            {
                "id": str(message.id),
                "to": message.to_number,
                "lead": message.lead.name if message.lead else "",
                "body": message.body,
                "message_type": message.message_type,
                "created_at": message.created_at.isoformat(),
                "available_at": "",
                "origin": origin,
                "priority": 1 if origin == "AI Engagement" else 2 if origin == "Auto Follow-up" else 3,
            }
        )

    items.sort(key=lambda item: (item.get("priority", 9), item.get("available_at") or item.get("created_at") or ""))
    return JsonResponse(
        {
            "ok": True,
            "phone_number": account.display_phone_number,
            "items": items,
        }
    )
