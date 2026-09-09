from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from apps.crm.models import Lead, Pipeline, Stage
from services.crm_activity_service import record_pipeline_changed, record_stage_changed


class LeadTransitionError(Exception):
    """Raised when a Lead stage/pipeline transition cannot be performed safely."""


def move_lead_to_stage(
    *,
    lead: Lead,
    stage: Stage,
    actor=None,
) -> Lead:
    """Move a Lead to an active Stage in its current Pipeline."""

    if lead is None:
        raise LeadTransitionError("Lead is required.")
    if not isinstance(stage, Stage):
        raise LeadTransitionError("Target stage must be a Stage instance.")
    if lead.pipeline is None or lead.organization_id != lead.pipeline.organization_id:
        raise LeadTransitionError("Lead pipeline does not belong to the lead's organization.")
    if not stage.is_active:
        raise LeadTransitionError("Target stage is inactive.")
    if stage.pipeline_id != lead.pipeline_id:
        raise LeadTransitionError("Target stage does not belong to the lead's pipeline.")
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
        raise LeadTransitionError("Lead stage transition validation failed.") from exc

    return lead


def move_lead_to_pipeline_stage(
    *,
    lead: Lead,
    pipeline: Pipeline,
    stage: Stage,
    actor=None,
) -> Lead:
    """Atomically move a lead to an active stage in another org-owned pipeline.

    The target objects are supplied by trusted application code. This function
    never resolves names or model-proposed identifiers itself.
    """

    if lead is None:
        raise LeadTransitionError("Lead is required.")
    if not isinstance(pipeline, Pipeline):
        raise LeadTransitionError("Target pipeline must be a Pipeline instance.")
    if not isinstance(stage, Stage):
        raise LeadTransitionError("Target stage must be a Stage instance.")
    if pipeline.organization_id != lead.organization_id:
        raise LeadTransitionError("Target pipeline does not belong to the lead's organization.")
    if not pipeline.is_active:
        raise LeadTransitionError("Target pipeline is inactive.")
    if not stage.is_active or stage.pipeline_id != pipeline.id:
        raise LeadTransitionError("Target stage does not belong to the active target pipeline.")
    if lead.pipeline_id == pipeline.id and lead.stage_id == stage.id:
        return lead
    if lead.pipeline_id == pipeline.id:
        return move_lead_to_stage(lead=lead, stage=stage, actor=actor)

    old_pipeline = lead.pipeline
    old_stage = lead.stage

    try:
        with transaction.atomic():
            lead.pipeline = pipeline
            lead.stage = stage
            lead.stage_entered_at = timezone.now()
            lead.full_clean()
            lead.save(
                update_fields=[
                    "pipeline",
                    "stage",
                    "stage_entered_at",
                    "updated_at",
                ]
            )
            record_pipeline_changed(
                lead=lead,
                actor=actor,
                old_pipeline=old_pipeline,
                new_pipeline=pipeline,
                old_stage=old_stage,
                new_stage=stage,
            )
    except DjangoValidationError as exc:
        raise LeadTransitionError("Lead pipeline transition validation failed.") from exc

    return lead
