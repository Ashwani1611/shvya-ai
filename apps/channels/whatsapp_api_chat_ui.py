"""Meta WhatsApp API-only inbox views.

Hosted Account chats are intentionally excluded. Hosted has its own linked-device
inbox under /dashboard/whatsapp/connect/hosted/.
"""

from django.contrib import messages
from django.db import models
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
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
from services.channels.whatsapp_error_service import message_failure_details
from services.channels.whatsapp_failure_patch import _failure_block
from services.channels.whatsapp_template_delivery import template_display_snapshot
from services.crm.lead_filter_service import active_filter_items, apply_lead_filters

from .models import WhatsAppAccount, WhatsAppTemplate, WhatsAppMessage
from .whatsapp_chat_smooth_ui import _inject_chat_ui


def _requested_account(request, lead):
    from django.core.exceptions import ValidationError
    from django.http import Http404
    account_id = request.POST.get("account") or request.GET.get("account")
    if not account_id:
        return None
    try:
        return get_object_or_404(WhatsAppAccount, pk=account_id,
                                organization_id=lead.organization_id, connection_type="api",
                                is_active=True, status="connected")
    except (ValidationError, ValueError):
        raise Http404("Invalid WhatsApp account")


def _lead_initials(lead):
    return "".join([p[0] for p in (lead.name or "").split()[:2]]).upper() or "?"


def _attach_template_display(chat_messages, *, organization):
    """Attach durable/full template presentation data to chat messages.

    New template sends persist a frozen display snapshot. Older messages are
    backfilled from the current organization-owned template when possible so
    previously sent footer/buttons also appear without a data migration.
    """
    template_ids = set()
    for message in chat_messages:
        message.template_display = None
        payload = message.media_payload if isinstance(message.media_payload, dict) else {}
        snapshot = payload.get("template_display")
        if isinstance(snapshot, dict) and snapshot:
            message.template_display = snapshot
            continue
        if payload.get("transport") == "template" and payload.get("template_id"):
            template_ids.add(str(payload["template_id"]))

    templates = {}
    if template_ids:
        templates = {
            str(template.id): template
            for template in WhatsAppTemplate.objects.filter(
                organization=organization,
                id__in=template_ids,
            ).select_related("meta_state")
        }

    for message in chat_messages:
        if message.template_display:
            continue
        payload = message.media_payload if isinstance(message.media_payload, dict) else {}
        if payload.get("transport") != "template":
            continue
        template = templates.get(str(payload.get("template_id") or ""))
        if template is not None:
            message.template_display = template_display_snapshot(
                template=template,
                rendered_body=message.body,
            )
        else:
            message.template_display = {
                "name": str(payload.get("template_name") or "Template"),
                "category": "",
                "format": "standard",
                "body": message.body or "",
                "footer": "",
                "attachment_type": "none",
                "buttons": [],
                "cards": [],
            }


def _chat_sidebar_context(request, user):
    account_id = request.GET.get("account")
    account = None
    if account_id:
        account = WhatsAppAccount.objects.filter(
            id=account_id,
            organization=user.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            status=WhatsAppAccount.Status.CONNECTED,
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

    all_conversations = apply_lead_filters(
        all_conversations,
        request.GET,
        user=user,
    )
    conversations = apply_lead_filters(
        conversations,
        request.GET,
        user=user,
    )

    query = (request.GET.get("q") or "").strip()
    if query:
        conversations = conversations.filter(
            models.Q(name__icontains=query)
            | models.Q(phone__icontains=query)
            | models.Q(email__icontains=query)
        )

    accounts = list_api_accounts(
        organization=user.organization,
        connected_only=True,
    )

    conversations = list(conversations)
    all_conversations = list(all_conversations)
    from apps.ai_engagement.services.intent_score import prepare_intent_scores
    prepare_intent_scores(conversations)
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
        "unlinked_conversations": _unlinked_conversations(user.organization, account, query),
        "accounts": accounts,
        "selected_account": account,
        "search_query": query,
        "active_tab": tab,
        "active_filters": active_filter_items(request.GET, user=user),
        "filter_query": request.GET.urlencode(),
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

    selected_account = _requested_account(request, lead)
    chat_messages = get_api_conversation_messages(
        organization=user.organization,
        lead=lead,
        account=selected_account,
    )
    if not selected_account and not chat_messages.exists():
        messages.error(request, "No active WhatsApp API conversation exists for this lead.")
        return redirect("whatsapp-chats")

    chat_messages = list(chat_messages)
    _attach_template_display(chat_messages, organization=user.organization)
    for message in chat_messages:
        if message.status == message.Status.FAILED:
            message.error = _failure_block(message_failure_details(message))

    conversation_account = selected_account or resolve_api_account_for_lead(
        organization=user.organization,
        lead=lead,
    )
    conversation_window_open = bool(
        conversation_account
        and is_within_api_24h_window(lead=lead, account=conversation_account)
    )

    mark_api_conversation_read(organization=user.organization, lead=lead, account=selected_account)

    lead.initials = _lead_initials(lead)
    lead.stage_color = (lead.stage.color if lead.stage_id else "") or "#9ca3af"

    context = _chat_sidebar_context(request, user)
    context.update(
        {
            "active_lead": lead,
            "chat_messages": chat_messages,
            "conversation_window_open": conversation_window_open,
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

    account = _requested_account(request, lead) or resolve_api_account_for_lead(
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

    if not is_within_api_24h_window(lead=lead, account=account):
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
        account__is_active=True,
        account__status=WhatsAppAccount.Status.CONNECTED,
        status=WhatsAppTemplate.Status.APPROVED,
    ).first()
    if not template:
        return JsonResponse({"error": "WhatsApp API template not found."}, status=404)

    account = _requested_account(request, lead) or resolve_api_account_for_lead(
        organization=user.organization,
        lead=lead,
    )
    if not account:
        return JsonResponse({"error": "No connected WhatsApp API account."}, status=400)

    if template.account_id != account.pk:
        return JsonResponse({"error": "Select a template for this WhatsApp number."}, status=400)
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


def _unlinked_conversations(organization, account=None, query=""):
    from django.db.models.functions import RowNumber, Substr

    rows = WhatsAppMessage.objects.filter(
        organization=organization,
        account__organization=organization,
        account__connection_type="api",
        account__is_active=True,
        lead__isnull=True,
    ).annotate(
        peer=models.Case(
            models.When(direction="inbound", then=models.F("from_number")),
            default=models.F("to_number"),
        ),
    )

    # Provider history can keep lead=NULL even after the same phone already
    # exists in CRM. Never offer a duplicate "Create lead" action for it.
    lead_phones = Lead.objects.filter(organization=organization)
    rows = rows.exclude(
        peer__in=models.Subquery(lead_phones.values("phone")),
    ).exclude(
        peer__in=models.Subquery(
            lead_phones.annotate(phone_without_plus=Substr("phone", 2)).values(
                "phone_without_plus"
            )
        ),
    )
    if account:
        rows = rows.filter(account=account)
    if query:
        rows = rows.filter(peer__icontains=query)
    return rows.annotate(rank=models.Window(expression=RowNumber(), partition_by=[models.F("account_id"), models.F("peer")],
        order_by=models.F("created_at").desc())).filter(rank=1).order_by("-created_at")[:100]


@crm_login_required
@require_GET
def unlinked_chat_view(request, account_id, message_id):
    from services.channels.chat_lead_service import chat_identity
    from django.core.exceptions import ValidationError
    from django.http import Http404
    account = get_object_or_404(WhatsAppAccount, pk=account_id, organization=request.crm_user.organization,
                               connection_type="api", is_active=True)
    try:
        phone, name, chat_messages = chat_identity(account=account, chat=str(message_id))
    except ValidationError:
        raise Http404("Conversation not found")
    lead = Lead.objects.filter(organization=request.crm_user.organization, phone=phone).first()
    if lead:
        from django.urls import reverse
        return redirect(reverse("whatsapp-chat-detail", args=[lead.pk]) + f"?account={account.pk}")
    context = _chat_sidebar_context(request, request.crm_user)
    context.update({"selected_account": account, "unlinked_contact": {"id": message_id, "name": name, "phone": phone},
                    "chat_messages": chat_messages.order_by("created_at")})
    return _inject_chat_ui(render(request, "channels/whatsapp_chat_list.html", context))
