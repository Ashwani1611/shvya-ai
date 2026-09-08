from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.followups.models import FollowupExecution, FollowupSequence, LeadSequenceState
from services.followup_service import resolve_linked_whatsapp_account


@crm_login_required
@require_GET
def lead_followup_control(request, lead_id):
    user = request.crm_user
    lead = get_object_or_404(
        Lead.objects.select_related("pipeline"),
        id=lead_id,
        organization=user.organization,
    )

    candidates = FollowupSequence.objects.filter(
        organization=user.organization,
        is_active=True,
        whatsapp_account__is_active=True,
    ).select_related("whatsapp_account").order_by("name")

    sequences = []
    for sequence in candidates:
        linked = resolve_linked_whatsapp_account(
            lead=lead,
            connection_type=sequence.whatsapp_account.connection_type,
        )
        if linked and (
            sequence.whatsapp_account.connection_type == "hosted"
            or linked.id == sequence.whatsapp_account_id
        ):
            sequences.append(sequence)

    current_state = (
        LeadSequenceState.objects.filter(
            lead=lead,
            status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
        )
        .select_related("sequence", "next_step", "assigned_by")
        .first()
    )
    latest_state = current_state or (
        LeadSequenceState.objects.filter(
            lead=lead,
        )
        .select_related("sequence", "next_step", "assigned_by")
        .order_by("-activated_at", "-assigned_at")
        .first()
    )

    return render(
        request,
        "followups/partials/lead_control.html",
        {
            "lead": lead,
            "sequences": sequences,
            "current_state": current_state,
            "display_state": (
                latest_state
                if latest_state and latest_state.status == LeadSequenceState.Status.COMPLETED
                else None
            ),
        },
    )


@crm_login_required
@require_GET
def lead_sequence_history(request, lead_id):
    user = request.crm_user
    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )
    states = list(
        LeadSequenceState.objects.filter(
            lead=lead,
            organization=user.organization,
        )
        .select_related("sequence", "assigned_by")
        .order_by("-activated_at", "-assigned_at")
    )
    executions = (
        FollowupExecution.objects.filter(
            lead=lead,
            organization=user.organization,
        )
        .select_related("sequence", "step", "state", "state__assigned_by")
        .order_by("-created_at")
    )
    return render(
        request,
        "followups/partials/lead_sequence_history.html",
        {"lead": lead, "sequence_states": states, "executions": executions},
    )
