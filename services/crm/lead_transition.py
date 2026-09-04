from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from apps.crm.models import Lead, Stage
from services.crm_activity_service import record_stage_changed


class LeadTransitionError(Exception):
    """
    Raised when a Lead stage transition cannot be performed safely.
    """


def move_lead_to_stage(
    *,
    lead: Lead,
    stage: Stage,
    actor=None,
) -> Lead:
    """
    Move a Lead to an active Stage in its current Pipeline.

    This service owns the reusable CRM transition lifecycle used by
    AI Engagement and, eventually, the CRM UI.

    Rules:
        - Lead must exist.
        - Target Stage must be a Stage instance.
        - Target Stage must be active.
        - Target Stage must belong to the Lead's current Pipeline.
        - Lead organization must match its Pipeline organization.
        - Same-stage movement is a no-op.
        - stage_entered_at is updated on an actual transition.
        - Lead validation is executed before persistence.
        - Stage activity is recorded after the mutation.
    """

    if lead is None:
        raise LeadTransitionError(
            "Lead is required."
        )

    if not isinstance(stage, Stage):
        raise LeadTransitionError(
            "Target stage must be a Stage instance."
        )

    if lead.organization_id != lead.pipeline.organization_id:
        raise LeadTransitionError(
            "Lead pipeline does not belong to the lead's organization."
        )

    if not stage.is_active:
        raise LeadTransitionError(
            "Target stage is inactive."
        )

    if stage.pipeline_id != lead.pipeline_id:
        raise LeadTransitionError(
            "Target stage does not belong to the lead's pipeline."
        )

    if lead.stage_id == stage.id:
        return lead

    old_stage = lead.stage
    pipeline = lead.pipeline

    try:
        with transaction.atomic():
            lead.stage = stage
            lead.stage_entered_at = timezone.now()

            lead.full_clean()

            lead.save(
                update_fields=[
                    "stage",
                    "stage_entered_at",
                    "updated_at",
                ]
            )

            record_stage_changed(
                lead=lead,
                actor=actor,
                pipeline=pipeline,
                old_stage=old_stage,
                new_stage=stage,
            )

    except DjangoValidationError as exc:
        raise LeadTransitionError(
            "Lead stage transition validation failed."
        ) from exc

    return lead