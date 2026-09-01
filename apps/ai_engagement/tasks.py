from __future__ import annotations

import logging

from celery import shared_task

from apps.ai_engagement.services.ai_provider import (
    AIProviderTransientError,
)
from apps.ai_engagement.services.internal_summary import (
    InternalSummaryError,
    InternalSummaryService,
)
from apps.ai_engagement.services.qualification import (
    QualificationError,
    QualificationService,
)
from apps.ai_engagement.services.summary_lock import (
    ConversationSummaryLock,
    SummaryLockError,
)

logger = logging.getLogger(__name__)


# ============================================================
# INTERNAL CONVERSATION SUMMARY
# ============================================================


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.generate_internal_conversation_summary",
)
def generate_internal_conversation_summary(
    self,
    lead_id: str,
):
    """
    Generate and publish the latest internal conversation
    summary for a Lead.

    The task receives only the Lead ID and resolves the
    current Lead state inside the worker.

    The task is intentionally idempotent at the summary-state
    level:

        - no Lead -> skip
        - no WhatsApp messages -> skip
        - current summary already covers latest message -> skip
        - another summary task is already running -> skip
        - stale summary -> generate/publish

    A per-lead Redis lock prevents concurrent workers from
    generating multiple summaries for the same Lead.
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
            "generate_internal_conversation_summary: "
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
    # SUMMARY SERVICE
    # --------------------------------------------------------

    service = InternalSummaryService()

    # --------------------------------------------------------
    # ACQUIRE PER-LEAD SUMMARY LOCK
    # --------------------------------------------------------

    summary_lock = ConversationSummaryLock(
        lead_id=lead.id,
    )

    try:

        if not summary_lock.acquire():

            logger.info(
                "generate_internal_conversation_summary: "
                "summary generation already in progress "
                "for lead %s",
                lead_id,
            )

            return {
                "status": "skipped",
                "reason": (
                    "summary_generation_in_progress"
                ),
                "lead_id": str(
                    lead_id
                ),
            }

        # ----------------------------------------------------
        # RE-READ LATEST WHATSAPP MESSAGE
        # ----------------------------------------------------

        latest_message = (
            lead.whatsapp_messages
            .filter(
                organization=lead.organization,
            )
            .order_by(
                "-created_at",
                "-id",
            )
            .first()
        )

        if latest_message is None:

            logger.info(
                "generate_internal_conversation_summary: "
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

        # ----------------------------------------------------
        # RE-CHECK SUMMARY FRESHNESS
        # ----------------------------------------------------

        if not service.is_summary_stale(
            organization=lead.organization,
            lead=lead,
            latest_message=latest_message,
        ):

            logger.info(
                "generate_internal_conversation_summary: "
                "summary already current for lead %s",
                lead_id,
            )

            current_summary = (
                service.get_current_summary(
                    organization=lead.organization,
                    lead=lead,
                )
            )

            return {
                "status": "skipped",
                "reason": "summary_current",
                "lead_id": str(
                    lead_id
                ),
                "summary_id": (
                    str(
                        current_summary.id
                    )
                    if current_summary
                    else None
                ),
                "source_last_message_id": (
                    str(
                        current_summary.source_last_message_id
                    )
                    if (
                        current_summary
                        and current_summary.source_last_message_id
                    )
                    else None
                ),
            }

        # ----------------------------------------------------
        # GENERATE + PUBLISH
        # ----------------------------------------------------

        try:

            summary = (
                service.generate_and_publish(
                    organization=lead.organization,
                    lead=lead,
                )
            )

        except AIProviderTransientError as exc:

            logger.warning(
                "generate_internal_conversation_summary: "
                "transient provider failure for lead %s: %s",
                lead_id,
                exc,
            )

            raise self.retry(
                exc=exc,
                countdown=60,
            )

        except InternalSummaryError as exc:

            logger.error(
                "generate_internal_conversation_summary: "
                "summary generation failed for lead %s: %s",
                lead_id,
                exc,
            )

            return {
                "status": "failed",
                "reason": "summary_generation_failed",
                "lead_id": str(
                    lead_id
                ),
                "error": str(
                    exc
                ),
            }

        except Exception as exc:

            logger.exception(
                "generate_internal_conversation_summary: "
                "unexpected failure for lead %s",
                lead_id,
            )

            raise self.retry(
                exc=exc,
            )

    except SummaryLockError as exc:

        logger.exception(
            "generate_internal_conversation_summary: "
            "summary lock failure for lead %s",
            lead_id,
        )

        raise self.retry(
            exc=exc,
            countdown=30,
        )

    finally:

        try:
            summary_lock.release()

        except SummaryLockError:

            logger.exception(
                "generate_internal_conversation_summary: "
                "failed to release summary lock for lead %s",
                lead_id,
            )

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    logger.info(
        "generate_internal_conversation_summary: "
        "published summary %s for lead %s",
        summary.id,
        lead_id,
    )

    return {
        "status": "completed",
        "lead_id": str(
            lead_id
        ),
        "summary_id": str(
            summary.id
        ),
        "source_message_count": (
            summary.source_message_count
        ),
        "source_last_message_id": (
            str(
                summary.source_last_message_id
            )
            if summary.source_last_message_id
            else None
        ),
        "source_last_message_at": (
            summary.source_last_message_at.isoformat()
            if summary.source_last_message_at
            else None
        ),
        "model_name": summary.model_name,
    }


# ============================================================
# LEAD QUALIFICATION
# ============================================================


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
    }