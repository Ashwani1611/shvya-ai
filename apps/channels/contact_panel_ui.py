"""Read-only, independently loaded CRM context shared by API/Coexistence/Instagram."""

from uuid import UUID

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
)
from apps.ai_engagement.services.intent_score import intent_score_for_lead
from services.followup_service import (
    available_sequences_for_lead,
    assign_sequence,
    FollowupError,
    resolve_linked_whatsapp_account,
    set_lead_followup_enabled,
)
from .models import WhatsAppAccount, WhatsAppTemplate


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
        if account
        and (
            (
                channel == "hosted"
                and s.provider == FollowupSequence.Provider.HOSTED
            )
            or (
                channel == "whatsapp"
                and s.provider == FollowupSequence.Provider.API
                and s.whatsapp_account_id == account.id
            )
        )
    ]
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
            "categories": TouchpointCategory.objects.filter(
                organization=lead.organization
            ).prefetch_related(
                Prefetch(
                    "replies",
                    queryset=TouchpointReply.objects.filter(is_active=True),
                )
            ),
        },
    )
    response["Cache-Control"] = "private, no-store"
    return response


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
    sequence_query = FollowupSequence.objects.filter(
        pk=sequence_id,
        organization=lead.organization,
        is_active=True,
    )
    if request.POST.get("channel") == "hosted":
        sequence_query = sequence_query.filter(
            provider=FollowupSequence.Provider.HOSTED,
        )
    else:
        sequence_query = sequence_query.filter(
            provider=FollowupSequence.Provider.API,
            whatsapp_account=account,
        )
    sequence = get_object_or_404(sequence_query)
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
