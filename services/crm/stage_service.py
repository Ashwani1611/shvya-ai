from django.core.exceptions import ValidationError

from apps.crm.models import Lead, Stage


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