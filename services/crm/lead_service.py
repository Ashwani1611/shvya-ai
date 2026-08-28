"""
LeadService — business logic for Lead creation/mutation lives here,
never in views or serializers.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction

from apps.crm.models import Lead
from services.crm_activity_service import record_lead_created


class DuplicateLeadError(Exception):
    """Raised when an upsert would violate (organization, phone) uniqueness
    in a way that isn't a normal update — e.g. a race condition."""


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

    return lead


def upsert_lead(*, organization, pipeline=None, stage=None, name, phone,
                 email="", notes="", attributes=None, lead_source="system"):
    """
    Create a Lead if (organization, phone) doesn't exist yet, otherwise
    update the existing one. Used by the Lead Upsert API and future
    Google Sheets import.

    Returns (lead, created: bool).

    Phone normalization and duplicate detection are handled entirely by
    Lead.clean() — this function does not duplicate that logic.
    """
    attributes = attributes or {}

    try:
        with transaction.atomic():
            existing = Lead.objects.filter(
                organization=organization, phone=phone
            ).first()

            if existing:
                if pipeline:
                    existing.pipeline = pipeline
                if stage:
                    existing.stage = stage
                existing.name = name or existing.name
                if email:
                    existing.email = email
                if notes:
                    existing.notes = notes
                if attributes:
                    existing.attributes = {**existing.attributes, **attributes}

                existing.full_clean()
                existing.save()
                return existing, False

            if pipeline is None or stage is None:
                raise DjangoValidationError(
                    "pipeline and stage are required when creating a new lead."
                )

            lead = Lead(
                organization=organization,
                pipeline=pipeline,
                stage=stage,
                name=name,
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

            return lead, True

    except IntegrityError as exc:
        raise DuplicateLeadError(
            "A lead with this phone number was created concurrently. Try again."
        ) from exc