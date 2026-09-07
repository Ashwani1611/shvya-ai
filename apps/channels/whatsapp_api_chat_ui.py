"""Meta WhatsApp API-only inbox views.

Hosted Account chats are intentionally excluded. Hosted has its own linked-device
inbox under /dashboard/whatsapp/connect/hosted/.
"""

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from services.channels.whatsapp_api_chat_service import (
    get_api_conversation_messages,
    is_within_api_24h_window,
    list_api_accounts,
    list_api_conversations,
    mark_api_conversation_read,
    resolve_api_account_for_lead,
)

from .models import WhatsAppAccount, WhatsAppTemplate
from .whatsapp_chat_failure_ui import _inject_chat_ui


def _lead_initials(lead):
    return "".join([p[0] for p in (lead.name or "").split()[:2]]).upper() or "?"


def _chat_sidebar_context(request, user):
    account_id = request.GET.get("account")
    account = None
    if account_id:
        account = WhatsAppAccount.objects.filter(
            id=account_id,
            organization=user.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            is_active=True,
        ).first()

    tab = request.GET.get("tab", "all")
    if tab not in ("all", "unread", "needs_reply", "failed", "broadcasts"):
        tab = "all"

    all_conversations = list_api_conversations(
        organization=user.organization,
        account=account,
    )
    conversations = list_api_conversations(
        organization=user.organization,
        account=account,
        tab=tab,
    )

    query = (request.GET.get("q") or "").strip()
    if query:
        query_lower = query.lower()
        conversations = [
            lead
            for lead in conversations
            if query_lower in (lead.name or "").lower()
            or query in (lead.phone or "")
        ]

    accounts = list_api_accounts(
        organization=user.organization,
        connected_only=True,
    )

    for lead in conversations:
        lead.initials = _lead_initials(lead)

    unread_count = sum(
        1 for lead in all_conversations if getattr(lead, "unread_count", 0) > 0
    )
    needs_reply_count = sum(
        1
        for lead in all_conversations
        if getattr(lead, "last_msg_direction", "") == "inbound"
    )
    failed_count = sum(
        1
        for lead in all_conversations
        if getattr(lead, "last_msg_status", "") == "failed"
    )

    return {
        "conversations": conversations,
        "accounts": accounts,
        "selected_account": account,
        "search_query": query,
        "active_tab": tab,
        "tab_counts": {
            "unread": unread_count,
            "needs_reply": needs_reply_count,
            "failed": failed_count,
        },
    }


@crm_login_required
@require_GET
def whatsapp_chat_list_view(request):
    context = _chat_sidebar_context(request, request.crm_user)
    context.update({"active_lead": None, "chat_messages": []})
    response = render(request, "channels/whatsapp_chat_list.html", context)
    return _inject_chat_ui(response)


@crm_login_required
@require_GET
def whatsapp_chat_detail_view(request, lead_id):
    user = request.crm_user
    lead = Lead.objects.filter(
        id=lead_id,
        organization=user.organization,
    ).select_related("pipeline", "stage", "organization").first()
    if not lead:
        messages.error(request, "Lead not found.")
        return redirect("whatsapp-chats")

    chat_messages = get_api_conversation_messages(
        organization=user.organization,
        lead=lead,
    )
    if not chat_messages.exists():
        messages.error(request, "No WhatsApp API conversation exists for this lead.")
        return redirect("whatsapp-chats")

    mark_api_conversation_read(organization=user.organization, lead=lead)

    lead.initials = _lead_initials(lead)
    lead.stage_color = (lead.stage.color if lead.stage_id else "") or "#9ca3af"

    lead_templates = WhatsAppTemplate.objects.filter(
        organization=user.organization,
        account__connection_type=WhatsAppAccount.ConnectionType.API,
        status=WhatsAppTemplate.Status.APPROVED,
    ).order_by("name")

    from apps.crm.models.call import LeadCall
    from apps.crm.models.note import LeadNote
    from apps.crm.models.stage import Stage

    context = _chat_sidebar_context(request, user)
    context.update(
        {
            "active_lead": lead,
            "chat_messages": chat_messages,
            "lead_templates": lead_templates,
            "lead_calls": LeadCall.objects.filter(lead=lead).order_by("-called_at")[:10],
            "lead_notes": LeadNote.objects.filter(lead=lead).order_by("-created_at")[:5],
            "lead_stages": Stage.objects.filter(
                pipeline=lead.pipeline,
                is_active=True,
            ).order_by("display_order"),
        }
    )
    response = render(request, "channels/whatsapp_chat_list.html", context)
    return _inject_chat_ui(response)


@crm_login_required
@require_POST
def whatsapp_send_message_view(request, lead_id):
    from apps.channels.tasks import send_whatsapp_message_task
    from services.channels.whatsapp_service import queue_outbound_message

    user = request.crm_user
    lead = Lead.objects.filter(
        id=lead_id,
        organization=user.organization,
    ).select_related("pipeline").first()
    if not lead:
        return JsonResponse({"error": "Lead not found."}, status=404)

    account = resolve_api_account_for_lead(
        organization=user.organization,
        lead=lead,
    )
    if not account:
        return JsonResponse(
            {"error": "No connected WhatsApp API account for this organization."},
            status=400,
        )

    body = (request.POST.get("body") or "").strip()
    if not body:
        return JsonResponse({"error": "Message body is required."}, status=400)

    if not is_within_api_24h_window(lead=lead):
        return JsonResponse(
            {
                "error": (
                    "This lead hasn't messaged the WhatsApp API number in the last "
                    "24 hours (or has never messaged it) -- send an approved template instead."
                )
            },
            status=400,
        )

    message = queue_outbound_message(
        organization=user.organization,
        account=account,
        to_number=lead.phone,
        body=body,
        lead=lead,
    )
    send_whatsapp_message_task.delay(str(message.id))
    return JsonResponse({"id": str(message.id), "status": message.status}, status=202)


@crm_login_required
@require_POST
def whatsapp_send_template_view(request, lead_id):
    from apps.channels.tasks import send_whatsapp_message_task
    from services.channels.template_service import render_template_body
    from services.channels.whatsapp_service import queue_outbound_message

    user = request.crm_user
    lead = Lead.objects.filter(
        id=lead_id,
        organization=user.organization,
    ).select_related("pipeline", "stage", "organization").first()
    if not lead:
        return JsonResponse({"error": "Lead not found."}, status=404)

    template_id = (request.POST.get("template_id") or "").strip()
    if not template_id:
        return JsonResponse({"error": "template_id is required."}, status=400)

    template = WhatsAppTemplate.objects.filter(
        id=template_id,
        organization=user.organization,
        account__connection_type=WhatsAppAccount.ConnectionType.API,
    ).first()
    if not template:
        return JsonResponse({"error": "WhatsApp API template not found."}, status=404)

    account = resolve_api_account_for_lead(
        organization=user.organization,
        lead=lead,
    )
    if not account:
        return JsonResponse({"error": "No connected WhatsApp API account."}, status=400)

    body = render_template_body(template=template, lead=lead)
    message = queue_outbound_message(
        organization=user.organization,
        account=account,
        to_number=lead.phone,
        body=body,
        lead=lead,
    )
    send_whatsapp_message_task.delay(str(message.id))
    return JsonResponse({"id": str(message.id), "status": message.status}, status=202)
