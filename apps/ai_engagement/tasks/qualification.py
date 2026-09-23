"""Lead qualification task boundary."""

import logging

from celery import shared_task
from django.db import transaction

from apps.ai_engagement.services.ai_provider import AIProviderTransientError
from apps.ai_engagement.services.qualification import (
    QualificationError,
    QualificationService,
)


logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.generate_lead_qualification",
)
def generate_lead_qualification(
    self,
    lead_id: str,
):
    """
    Generate and append the latest AI qualification summary
    for a Lead.

    The task receives only the Lead ID and resolves the
    current Lead state inside the worker.

    Behavior:

        - no Lead
            -> skip

        - no WhatsApp messages
            -> skip

        - qualification result unchanged
            -> skip

        - qualification changed
            -> append a new qualification update

    Qualification is persisted through the existing LeadNote
    model using note_type="system".

    The qualification service is responsible for:
        - AI context construction
        - Conversation Summary reference
        - qualification generation
        - meaningful-change detection
        - LeadNote persistence
    """

    from apps.crm.models import Lead

    # --------------------------------------------------------
    # RESOLVE LEAD
    # --------------------------------------------------------

    try:

        lead = (
            Lead.objects
            .select_related(
                "organization",
                "pipeline",
                "stage",
            )
            .get(
                id=lead_id,
            )
        )

    except Lead.DoesNotExist:

        logger.warning(
            "generate_lead_qualification: "
            "lead %s not found",
            lead_id,
        )

        return {
            "status": "skipped",
            "reason": "lead_not_found",
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # QUALIFICATION SERVICE
    # --------------------------------------------------------

    service = QualificationService()

    # --------------------------------------------------------
    # VERIFY CONVERSATION EXISTS
    # --------------------------------------------------------

    has_message = (
        lead.whatsapp_messages
        .filter(
            organization=lead.organization,
        )
        .exists()
    )

    if not has_message:

        logger.info(
            "generate_lead_qualification: "
            "no WhatsApp messages for lead %s",
            lead_id,
        )

        return {
            "status": "skipped",
            "reason": "no_messages",
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # GENERATE + APPEND
    # --------------------------------------------------------

    try:

        # Semantic criteria use a signed, current-evidence receipt. Provider
        # work happens here in the background, outside every CRM transaction.
        from apps.ai_engagement.services.qualification_check import QualificationCheckService, QualificationCheckError
        from apps.ai_engagement.services.semantic_criteria import apply_verified_semantic_completion
        try:
            criteria_verification = QualificationCheckService().refresh_semantic(
                organization=lead.organization, lead=lead,
            )
        except QualificationCheckError:
            logger.warning("Qualification criteria could not be verified for lead %s", lead_id)
            criteria_verification = {"status": "unresolved", "reason": "criteria_verification_failed"}
        if criteria_verification.get("status") in {"verified", "unchanged"}:
            criteria_verification["completion"] = apply_verified_semantic_completion(
                organization=lead.organization, lead=lead,
            )

        note = (
            service.generate_and_append(
                organization=lead.organization,
                lead=lead,
            )
        )

    except AIProviderTransientError as exc:

        logger.warning(
            "generate_lead_qualification: "
            "transient provider failure for lead %s: %s",
            lead_id,
            exc,
        )

        raise self.retry(
            exc=exc,
            countdown=60,
        )

    except QualificationError as exc:

        logger.error(
            "generate_lead_qualification: "
            "qualification generation failed "
            "for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": (
                "qualification_generation_failed"
            ),
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    except Exception as exc:

        logger.exception(
            "generate_lead_qualification: "
            "unexpected failure for lead %s",
            lead_id,
        )

        raise self.retry(
            exc=exc,
        )

    # --------------------------------------------------------
    # NO MEANINGFUL CHANGE
    # --------------------------------------------------------

    if note is None:

        logger.info(
            "generate_lead_qualification: "
            "qualification unchanged for lead %s",
            lead_id,
        )

        return {
            "status": "skipped",
            "reason": (
                "qualification_unchanged"
            ),
            "criteria_verification": criteria_verification,
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    logger.info(
        "generate_lead_qualification: "
        "qualification updated for lead %s "
        "using note %s",
        lead_id,
        note.id,
    )

    return {
        "status": "completed",
        "lead_id": str(
            lead_id
        ),
        "note_id": str(
            note.id
        ),
        "note_type": note.note_type,
        "criteria_verification": criteria_verification,
    }
