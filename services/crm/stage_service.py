from django.core.exceptions import ValidationError

from apps.crm.models import Stage


def move_lead(*, lead, new_stage):
    if not isinstance(new_stage, Stage):
        raise ValidationError("new_stage must be a Stage instance.")

    if new_stage.pipeline_id != lead.pipeline_id:
        raise ValidationError(
            "Stage does not belong to the lead's pipeline."
        )

    lead.stage = new_stage
    lead.save(update_fields=["stage", "updated_at"])

    return lead


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