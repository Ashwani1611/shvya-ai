"""Read-only, independently loaded CRM context shared by API/Coexistence/Instagram."""

from uuid import UUID

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST
from django.db import transaction

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead, Pipeline
from apps.followups.models import (
    FollowupSequence,
    LeadSequenceState,
    TouchpointCategory,
)
from apps.ai_engagement.services.intent_score import intent_score_for_lead
from services.followup_service import (
    available_sequences_for_lead,
    assign_sequence,
    FollowupError,
    resolve_linked_whatsapp_account,
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
    channel = "instagram" if request.GET.get("channel") == "instagram" else "whatsapp"
    account = linked_api(lead) if channel == "whatsapp" else None
    requested = request.GET.get("account")
    if requested and (not account or str(account.pk) != requested):
        account = None
    sequences = [
        s
        for s in available_sequences_for_lead(lead=lead)
        if account and s.whatsapp_account_id == account.id
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
            if account
            else [],
            "lead_calls": lead.calls.all()[:50],
            "categories": TouchpointCategory.objects.filter(
                organization=lead.organization
            ).prefetch_related("replies"),
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
    account = linked_api(lead)
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
