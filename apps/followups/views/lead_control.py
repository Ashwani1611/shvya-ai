from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.followups.models import FollowupSequence, LeadSequenceState


@crm_login_required
@require_GET
def lead_followup_control(request, lead_id):
    user = request.crm_user
    lead = get_object_or_404(
        Lead.objects.select_related("pipeline"),
        id=lead_id,
        organization=user.organization,
    )

    sequences = FollowupSequence.objects.filter(
        organization=user.organization,
        is_active=True,
        whatsapp_account__is_active=True,
    ).select_related("whatsapp_account").order_by("name")

    # Prefer sequences using the number mapped to the lead's pipeline. If the
    # pipeline has no sender configured, keep every connected sequence visible.
    pipeline_number = getattr(lead.pipeline, "phone_number", "") if lead.pipeline_id else ""
    if pipeline_number:
        sequences = sequences.filter(
            Q(whatsapp_account__display_phone_number=pipeline_number)
            | Q(whatsapp_account__phone_number_id=pipeline_number)
        )

    current_state = (
        LeadSequenceState.objects.filter(
            lead=lead,
            status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
        )
        .select_related("sequence", "next_step")
        .first()
    )

    return render(
        request,
        "followups/partials/lead_control.html",
        {
            "lead": lead,
            "sequences": sequences,
            "current_state": current_state,
        },
    )
