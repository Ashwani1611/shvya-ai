from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.followups.models import FollowupExecution, LeadSequenceState
from services.followup_service import available_sequences_for_lead


@crm_login_required
@require_GET
def lead_followup_control(request, lead_id):
    user = request.crm_user
    lead = get_object_or_404(
        Lead.objects.select_related("pipeline"),
        id=lead_id,
        organization=user.organization,
    )

    sequences = available_sequences_for_lead(lead=lead)

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
