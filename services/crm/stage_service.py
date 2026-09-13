from django.core.exceptions import ValidationError

from apps.crm.models import Stage
from services.crm.lead_transition import LeadTransitionError, move_lead_to_stage


def move_lead(*, lead, new_stage):
    """Compatibility wrapper for callers that advance a lead within a pipeline.

    All stage changes are persisted through the canonical transition service so
    stage_entered_at and LeadActivity history stay consistent with the Lead row.
    """
    if not isinstance(new_stage, Stage):
        raise ValidationError("new_stage must be a Stage instance.")

    if new_stage.pipeline_id != lead.pipeline_id:
        raise ValidationError(
            "Stage does not belong to the lead's pipeline."
        )

    try:
        return move_lead_to_stage(
            lead=lead,
            stage=new_stage,
            actor=None,
        )
    except LeadTransitionError as exc:
        raise ValidationError(str(exc)) from exc


def move_to_next_stage(*, lead):
    """
    Advance a lead to the next stage in its pipeline, ordered by
    Stage.display_order. No-op (returns the lead unchanged) if the
    lead has no stage yet, or is already at the last stage.

    Used by the WhatsApp reply-intent flow: a positive reply
    ("yes" / "+" / etc.) auto-advances the lead one step instead
    of requiring an agent to do it manually.
    """
    if not lead.stage_id:
        return lead

    next_stage = (
        Stage.objects.filter(
            pipeline_id=lead.pipeline_id,
            is_active=True,
            display_order__gt=lead.stage.display_order,
        )
        .order_by("display_order")
        .first()
    )

    if not next_stage:
        # Already at the last stage -- nothing to advance to.
        return lead

    return move_lead(lead=lead, new_stage=next_stage)
