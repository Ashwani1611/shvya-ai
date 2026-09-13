"""
LeadService — business logic for Lead creation/mutation lives here,
never in views or serializers.
"""
import re

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction

from apps.crm.models import Lead
from services.crm_activity_service import record_lead_created
from services.crm.lead_transition import (
    LeadTransitionError,
    move_lead_to_pipeline_stage,
    move_lead_to_stage,
)


class DuplicateLeadError(Exception):
    """Raised when an upsert would violate (organization, phone) uniqueness
    in a way that isn't a normal update — e.g. a race condition."""


def _schedule_new_lead_welcome(lead):
    """Queue welcome orchestration only for New Lead(s), after commit."""
    stage_name = str(getattr(lead.stage, "name", "") or "").strip().casefold()
    if stage_name not in {"new lead", "new leads"}:
        return

    from apps.channels.welcome_tasks import send_lead_welcome_task

    lead_id = str(lead.id)
    transaction.on_commit(
        lambda lead_id=lead_id: send_lead_welcome_task.delay(lead_id)
    )


def create_lead(*, organization, pipeline, stage, name, phone, **extra_fields):
    """
    Create a Lead. Raises DjangoValidationError (from Lead.clean()) if
    the phone is malformed or already exists for this organization.
    """
    lead = Lead(
        organization=organization,
        pipeline=pipeline,
        stage=stage,
        name=name,
        phone=phone,
        **extra_fields,
    )
    lead.full_clean()
    lead.save()

    record_lead_created(
        lead=lead,
        actor=None,
    )
    _schedule_new_lead_welcome(lead)

    return lead


def _looks_like_phone_name(name, phone):
    """Return True when a supplied name is really just the phone number."""
    name_digits = re.sub(r"\D", "", str(name or ""))
    phone_digits = re.sub(r"\D", "", str(phone or ""))
    return bool(name_digits and phone_digits and name_digits == phone_digits)


def upsert_lead(*, organization, pipeline=None, stage=None, name, phone,
                 email="", notes="", attributes=None, lead_source="system"):
    """
    Create a Lead if (organization, phone) doesn't exist yet, otherwise
    update the existing one. Used by the Lead Upsert API and future
    Google Sheets import.

    Returns (lead, created: bool).

    Phone normalization and duplicate detection are handled entirely by
    Lead.clean() — this function does not duplicate that logic.

    WhatsApp inbound messages are intentionally conservative: an incoming
    message must never replace a human-managed CRM name, pipeline, or stage.
    Meta currently gives the WhatsApp handler the sender number as its
    fallback name, so a repeated message must not turn e.g. "Rahul Kumar"
    into "9198...". New WhatsApp-only leads get a readable placeholder
    instead of displaying the phone number as their name.

    Existing-lead pipeline/stage changes are always delegated to the shared
    transition service so the database row and LeadActivity history are
    committed together. No caller is allowed to create a UI-only move.
    """
    attributes = attributes or {}
    is_whatsapp_inbound = lead_source == "whatsapp_api"

    try:
        with transaction.atomic():
            existing = (
                Lead.objects.select_for_update()
                .filter(organization=organization, phone=phone)
                .select_related("pipeline", "stage")
                .first()
            )

            if existing:
                transition = None

                if not is_whatsapp_inbound:
                    existing.name = name or existing.name

                    if pipeline and pipeline.id != existing.pipeline_id:
                        if stage is None:
                            raise DjangoValidationError(
                                {
                                    "stage": (
                                        "Stage is required when moving a lead "
                                        "to another pipeline."
                                    )
                                }
                            )
                        transition = ("pipeline", pipeline, stage)
                    elif stage and stage.id != existing.stage_id:
                        transition = ("stage", stage)

                if email:
                    existing.email = email
                if notes:
                    existing.notes = notes
                if attributes:
                    existing.attributes = {**existing.attributes, **attributes}

                # Validate and persist non-routing changes first. The enclosing
                # transaction guarantees they roll back if the transition fails.
                existing.full_clean()
                existing.save()

                try:
                    if transition and transition[0] == "pipeline":
                        _, target_pipeline, target_stage = transition
                        move_lead_to_pipeline_stage(
                            lead=existing,
                            pipeline=target_pipeline,
                            stage=target_stage,
                            actor=None,
                        )
                    elif transition and transition[0] == "stage":
                        _, target_stage = transition
                        move_lead_to_stage(
                            lead=existing,
                            stage=target_stage,
                            actor=None,
                        )
                except LeadTransitionError as exc:
                    raise DjangoValidationError(str(exc)) from exc

                return existing, False

            if pipeline is None or stage is None:
                raise DjangoValidationError(
                    "pipeline and stage are required when creating a new lead."
                )

            lead_name = name
            if is_whatsapp_inbound and (
                not str(name or "").strip()
                or _looks_like_phone_name(name, phone)
            ):
                lead_name = "WhatsApp Lead"

            lead = Lead(
                organization=organization,
                pipeline=pipeline,
                stage=stage,
                name=lead_name,
                phone=phone,
                email=email,
                notes=notes,
                attributes=attributes,
                lead_source=lead_source,
            )
            lead.full_clean()
            lead.save()

            record_lead_created(
                lead=lead,
                actor=None,
            )
            _schedule_new_lead_welcome(lead)

            return lead, True

    except IntegrityError as exc:
        raise DuplicateLeadError(
            "A lead with this phone number was created concurrently. Try again."
        ) from exc
