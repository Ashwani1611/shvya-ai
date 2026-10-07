"""Read-only, independently loaded CRM context shared by API/Coexistence/Instagram."""

from uuid import UUID

from django.core.files import File

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.db import transaction, IntegrityError
from django.db.models import Prefetch
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead, Pipeline
from apps.followups.models import (
    FollowupSequence,
    LeadSequenceState,
    TouchpointCategory,
    TouchpointReply,
    TouchpointAttachment,
)
from apps.ai_engagement.services.intent_score import intent_score_for_lead
from services.followup_service import (
    available_sequences_for_lead,
    assign_sequence,
    FollowupError,
    resolve_linked_whatsapp_account,
    set_lead_followup_enabled,
)
from .models import WhatsAppAccount, WhatsAppTemplate, WhatsAppMessage
from services.touchpoint_service import render_touchpoint, attachment_kind, placeholder_keys


def linked_api(lead):
    return resolve_linked_whatsapp_account(
        lead=lead, connection_type=WhatsAppAccount.ConnectionType.API
    )


@crm_login_required
@require_GET
def contact_panel(request, lead_id):
    lead = get_object_or_404(
        Lead.objects.select_related(
            "pipeline", "stage", "organization"
        ).prefetch_related("lead_notes"),
        pk=lead_id,
        organization=request.crm_user.organization,
    )
    channel = request.GET.get("channel", "whatsapp")
    if channel not in {"whatsapp", "hosted", "instagram"}:
        channel = "whatsapp"
    account = resolve_linked_whatsapp_account(
        lead=lead, connection_type="hosted" if channel == "hosted" else "api"
    ) if channel != "instagram" else None
    requested = request.GET.get("account")
    if requested and (not account or str(account.pk) != requested):
        account = None
    sequences = [
        s
        for s in available_sequences_for_lead(lead=lead)
        if account and s.whatsapp_account_id == account.id
    ]
    from apps.shvya_calendar.booking_presentation import attach_booking_links_to_leads
    attach_booking_links_to_leads([lead], organization=request.crm_user.organization)

    categories = list(TouchpointCategory.objects.filter(
        organization=lead.organization,
    ).prefetch_related(Prefetch(
        "replies",
        queryset=TouchpointReply.objects.filter(is_active=True).prefetch_related(
            "attachments"
        ),
    )))
    # Compute the organisation's allowed keys and this lead's current values
    # once, not once per quick reply in a potentially large library.
    from services.followup_service import _lead_template_values
    allowed_keys = placeholder_keys(lead.organization)
    lead_values = _lead_template_values(lead, request.crm_user)
    for category in categories:
        for reply in category.replies.all():
            reply.personalized_body, reply.missing_placeholders = render_touchpoint(
                reply=reply, lead=lead, allowed_keys=allowed_keys, values=lead_values,
            )

    response = render(
        request,
        "channels/contact_panel.html",
        {
            "active_lead": lead,
            "channel": channel,
            "linked_account": account,
            "intent_score": intent_score_for_lead(lead=lead),
            "pipelines": Pipeline.objects.filter(
                organization=lead.organization, is_active=True
            ).order_by("name"),
            "lead_stages": lead.pipeline.stages.filter(is_active=True).order_by(
                "display_order"
            ),
            "sequence_state": LeadSequenceState.objects.filter(
                organization=lead.organization,
                lead=lead,
                status__in=["active", "paused"],
            )
            .select_related("sequence")
            .first(),
            "sequences": sequences,
            "lead_templates": WhatsAppTemplate.objects.filter(
                organization=lead.organization, account=account
            ).order_by("name")
            if account and channel == "whatsapp"
            else [],
            "lead_calls": lead.calls.all()[:50],
            "categories": categories,
            "can_send_touchpoint_files": bool(account) and channel in {"hosted", "whatsapp"},
        },
    )
    response["Cache-Control"] = "private, no-store"
    return response


@crm_login_required
@require_POST
def send_touchpoint_attachment(request, lead_id, attachment_id):
    """Explicit agent action: send one saved attachment via this lead's channel.

    Sending is never triggered by opening/previewing a Touchpoint. WhatsApp API
    and Hosted use their existing policy-checked, durable outbound transports.
    """
    from services.followup_service import resolve_linked_whatsapp_account

    organization = request.crm_user.organization
    lead = get_object_or_404(
        Lead.objects.select_related("pipeline", "organization"),
        pk=lead_id, organization=organization,
    )
    attachment = get_object_or_404(
        TouchpointAttachment.objects.select_related("reply__category"),
        pk=attachment_id,
        reply__category__organization=organization,
        reply__is_active=True,
    )
    channel = str(request.POST.get("channel") or "").strip()
    if channel not in {"whatsapp", "hosted"}:
        return JsonResponse({"error": "This channel does not support direct Touchpoint file sending."}, status=400)
    expected_connection = (
        WhatsAppAccount.ConnectionType.coexisted
        if channel == "hosted" else WhatsAppAccount.ConnectionType.API
    )
    account = resolve_linked_whatsapp_account(
        lead=lead, connection_type=expected_connection,
    )
    if (
        not account or not account.is_active
        or account.status != WhatsAppAccount.Status.CONNECTED
        or str(account.pk) != str(request.POST.get("account") or "")
        or account.organization_id != organization.pk
    ):
        return JsonResponse({"error": "Use the connected number linked to this lead's pipeline."}, status=400)
    if not lead.phone or not attachment.file:
        return JsonResponse({"error": "Lead number or Touchpoint attachment is unavailable."}, status=400)
    kind = attachment_kind(attachment.original_name)
    if channel == "whatsapp":
        from apps.channels.tasks import send_whatsapp_message_task
        from services.channels.whatsapp_api_chat_service import is_within_api_24h_window
        from services.channels.whatsapp_service import queue_outbound_message

        if not is_within_api_24h_window(lead=lead, account=account):
            return JsonResponse(
                {"error": "The 24-hour WhatsApp API reply window has closed. Send an approved template."},
                status=400,
            )
        message = queue_outbound_message(
            organization=organization, account=account, to_number=lead.phone,
            lead=lead, body="", message_type=getattr(WhatsAppMessage.MessageType, kind.upper()),
            media_payload={"source": "touchpoint", "attachment_id": str(attachment.id)},
        )
        send_whatsapp_message_task.delay(str(message.id))
        return JsonResponse({"ok": True, "status": message.status, "message": "Touchpoint file queued."}, status=202)

    from apps.channels.hosted_send_ui import _send_manual_response
    from services.channels.hosted_send_service import queue_hosted_uploaded_media
    from services.channels.hosted_whatsapp_service import HostedWhatsAppValidationError

    try:
        attachment.file.open("rb")
        try:
            wrapped = File(attachment.file.file, name=attachment.original_name)
            wrapped.content_type = attachment.mime_type
            message = queue_hosted_uploaded_media(
                account=account, to_number=lead.phone, uploaded_file=wrapped,
                message_type=getattr(WhatsAppMessage.MessageType, kind.upper()),
                caption="", lead=lead,
            )
        finally:
            attachment.file.close()
    except (HostedWhatsAppValidationError, OSError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return _send_manual_response(message=message, chat_key=lead.phone)


@crm_login_required
@require_POST
@transaction.atomic
def start_checking_in(request, lead_id):
    lead = get_object_or_404(
        Lead.objects.select_for_update().select_related("pipeline", "organization"),
        pk=lead_id,
        organization=request.crm_user.organization,
    )
    account = resolve_linked_whatsapp_account(
        lead=lead, connection_type="hosted" if request.POST.get("channel") == "hosted" else "api"
    )
    if not account or (
        request.POST.get("account") and request.POST["account"] != str(account.pk)
    ):
        return JsonResponse(
            {"error": "Use the connected number linked to this lead's pipeline."},
            status=400,
        )
    try:
        sequence_id = UUID(request.POST.get("sequence", ""))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Choose a valid sequence."}, status=400)
    sequence = get_object_or_404(
        FollowupSequence,
        pk=sequence_id,
        organization=lead.organization,
        whatsapp_account=account,
        is_active=True,
    )
    if not sequence.steps.filter(is_active=True).exists():
        return JsonResponse(
            {"error": "Add an active step to this sequence first."}, status=400
        )
    try:
        state = assign_sequence(lead=lead, sequence=sequence, actor=request.crm_user)
    except FollowupError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(
        {
            "ok": True,
            "sequence": state.sequence.name,
            "message": "Sequence started. Steps follow the configured schedule and sending rules.",
        }
    )


@crm_login_required
@require_POST
def toggle_followups(request, lead_id):
    lead = get_object_or_404(Lead, pk=lead_id, organization=request.crm_user.organization)
    value = request.POST.get("enabled")
    if value not in {"true", "false"}:
        return JsonResponse({"error": "Choose on or off."}, status=400)
    set_lead_followup_enabled(lead=lead, enabled=value == "true")
    return JsonResponse({"ok": True, "enabled": lead.auto_followup_enabled})


@crm_login_required
@require_http_methods(["GET", "POST"])
def unlinked_contact(request, account_id):
    from services.channels.chat_lead_service import chat_identity, creation_pipelines, create_chat_lead
    account = get_object_or_404(WhatsAppAccount, pk=account_id, organization=request.crm_user.organization, is_active=True)
    chat = request.GET.get("chat") or request.POST.get("chat", "")
    channel = "hosted" if account.connection_type == "hosted" else "whatsapp"
    try:
        phone, name, _ = chat_identity(account=account, chat=chat)
        if request.method == "POST":
            lead = create_chat_lead(user=request.crm_user, account=account, chat=chat,
                                    name=request.POST.get("name", ""), pipeline_id=request.POST.get("pipeline", ""))
            result = {"ok": True, "sidebar_url": reverse("chat-contact-panel", args=[lead.pk]) + f"?channel={channel}&account={account.pk}"}
            if channel == "whatsapp":
                result["redirect_url"] = reverse("whatsapp-chat-detail", args=[lead.pk]) + f"?account={account.pk}"
            return JsonResponse(result)
        lead = Lead.objects.filter(organization=request.crm_user.organization, phone=phone).first() if phone else None
        if lead:
            return redirect(reverse("chat-contact-panel", args=[lead.pk]) + f"?channel={channel}&account={account.pk}")
        return render(request, "channels/contact_unlinked.html", {
            "phone": phone, "contact_name": name, "chat": chat, "can_create": bool(phone),
            "pipelines": creation_pipelines(user=request.crm_user, account=account),
        })
    except (ValidationError, ValueError) as exc:
        return JsonResponse({"error": " ".join(exc.messages) if isinstance(exc, ValidationError) else "Choose a valid conversation."}, status=400)
    except IntegrityError:
        return JsonResponse({"error": "This contact changed while saving. Reload and retry."}, status=409)


@crm_login_required
@require_http_methods(["GET", "POST"])
def instagram_contact(request, conversation_id):
    from .instagram_models import InstagramConversation
    from services.channels.instagram_leads import link_instagram_lead
    from services.crm.lead_filter_service import accessible_pipelines
    conversation = get_object_or_404(InstagramConversation, pk=conversation_id, organization=request.crm_user.organization)
    if request.method == "POST":
        try:
            conversation = link_instagram_lead(
                user=request.crm_user,
                conversation_id=conversation.pk,
                phone=request.POST.get("phone", ""),
                name=request.POST.get("name", ""),
                pipeline_id=request.POST.get("pipeline", ""),
            )
            return JsonResponse({
                "ok": True,
                "sidebar_url": reverse(
                    "chat-contact-panel", args=[conversation.lead_id]
                ) + "?channel=instagram",
            })
        except (ValidationError, ValueError, IntegrityError) as exc:
            return JsonResponse(
                {
                    "error": (
                        " ".join(exc.messages)
                        if isinstance(exc, ValidationError)
                        else "Check the lead details and pipeline, then retry."
                    )
                },
                status=400,
            )
    if conversation.lead_id:
        return redirect(reverse("chat-contact-panel", args=[conversation.lead_id]) + "?channel=instagram")
    return render(request, "channels/contact_unlinked.html", {"instagram": True, "can_create": True,
        "contact_name": conversation.participant_username or conversation.participant_name,
        "pipelines": accessible_pipelines(request.crm_user)})
