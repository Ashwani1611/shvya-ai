from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

from datetime import timedelta

from django.utils import timezone

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

# ============================================================
# WHATSAPP AI ENGAGEMENT
# ============================================================


def _latest_whatsapp_message(*, lead):
    """Return the latest WhatsApp message for this Lead."""
    return (
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


def _has_existing_ai_response(
    *,
    lead,
    inbound_message,
    body,
):
    """
    Detect whether an AI-generated outbound WhatsApp response
    already exists for the given inbound message.

    Primary protection:
        raw_payload["shvya_ai"]["source_inbound_message_id"]

    Fallback protection:
        same outbound body created at or after the source
        inbound message timestamp.
    """
    from apps.channels.models import WhatsAppMessage

    if inbound_message is None:
        return False

    source_id = str(
        inbound_message.id
    )

    # Primary idempotency check:
    # the outbound message explicitly records which inbound
    # message caused the AI response.
    if (
        WhatsAppMessage.objects
        .filter(
            organization=lead.organization,
            lead=lead,
            direction=(
                WhatsAppMessage.Direction.OUTBOUND
            ),
            raw_payload__shvya_ai__source_inbound_message_id=(
                source_id
            ),
        )
        .exists()
    ):
        return True

    # Defensive fallback for outbound messages created before
    # the SHVYA AI metadata was attached.
    if not body:
        return False

    return (
        WhatsAppMessage.objects
        .filter(
            organization=lead.organization,
            lead=lead,
            direction=(
                WhatsAppMessage.Direction.OUTBOUND
            ),
            body=body,
            created_at__gte=inbound_message.created_at,
        )
        .exists()
    )

def _whatsapp_send_eligible(
    *,
    lead,
    inbound_message,
    account,
):
    """
    Deterministic eligibility check for an AI-generated free-form
    WhatsApp response.

    AI does not decide whether the transport is technically allowed.

    Requirements:
        - Lead must have a phone number.
        - WhatsApp account must exist.
        - Account must belong to the Lead's organization.
        - Account must be active.
        - Account must be connected.
        - Latest source message must be inbound.
        - Source inbound message must still be inside the 24-hour
          customer-service window.

    This function does not send anything.
    """

    from apps.channels.models import WhatsAppAccount

    if not (
        lead.phone
        or ""
    ).strip():
        return (
            False,
            "lead_has_no_phone_number",
        )

    if account is None:
        return (
            False,
            "no_whatsapp_account",
        )

    if (
        account.organization_id
        != lead.organization_id
    ):
        return (
            False,
            "organization_mismatch",
        )

    if not account.is_active:
        return (
            False,
            "whatsapp_account_inactive",
        )

    if (
        account.status
        != WhatsAppAccount.Status.CONNECTED
    ):
        return (
            False,
            "whatsapp_account_not_connected",
        )

    if (
        inbound_message is None
        or inbound_message.direction
        != inbound_message.Direction.INBOUND
    ):
        return (
            False,
            "source_message_not_inbound",
        )

    if (
        inbound_message.created_at
        is None
    ):
        return (
            False,
            "source_message_missing_timestamp",
        )

    expires_at = (
        inbound_message.created_at
        + timedelta(
            hours=24,
        )
    )

    if timezone.now() >= expires_at:
        return (
            False,
            "whatsapp_24h_window_expired",
        )

    return (
        True,
        "eligible",
    )

def _execute_ai_engagement_response(
    *,
    task,
    lead_id: str,
):
    """
    Shared AI Engagement execution path.

    The canonical production task receives only the Lead ID and
    resolves the current Lead, organization, pipeline, stage, and
    connected WhatsApp account inside the worker.

    The execution order is:

        Lead
        -> AI permission
        -> WhatsApp account
        -> latest inbound message
        -> AI Engagement decision
        -> permission re-check
        -> conversation freshness re-check
        -> final transactional lock
        -> duplicate protection
        -> WhatsApp send eligibility
        -> CRM actions
        -> queue outbound WhatsApp message
        -> dispatch existing WhatsApp sender after commit

    This function does not call Meta directly.
    """

    from apps.ai_engagement.services.ai_permissions import (
        AIPermissionError,
        AIPermissionService,
    )
    from apps.ai_engagement.services.crm_executor import (
        CRMActionExecutionError,
        CRMActionExecutor,
    )
    from apps.ai_engagement.services.engagement import (
        EngagementError,
        EngagementService,
    )
    from apps.channels.models import (
        WhatsAppMessage,
    )
    from apps.channels.tasks import (
        send_whatsapp_message_task,
    )
    from apps.crm.models import Lead
    from services.channels.whatsapp_service import (
        queue_outbound_message,
        resolve_account_for_lead,
    )

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
            "generate_ai_engagement_response: "
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

    organization = lead.organization

    # --------------------------------------------------------
    # INITIAL AI PERMISSION CHECK
    # --------------------------------------------------------

    try:
        permission = (
            AIPermissionService().evaluate(
                organization=organization,
                lead=lead,
            )
        )

    except AIPermissionError as exc:
        logger.error(
            "generate_ai_engagement_response: "
            "permission evaluation failed for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": (
                "ai_permission_evaluation_failed"
            ),
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    if not permission.allowed:
        return {
            "status": "skipped",
            "reason": permission.reason,
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # RESOLVE WHATSAPP ACCOUNT
    # --------------------------------------------------------

    account = resolve_account_for_lead(
        organization=organization,
        lead=lead,
    )

    if account is None:
        return {
            "status": "skipped",
            "reason": "no_connected_whatsapp_account",
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # RESOLVE SOURCE INBOUND MESSAGE
    # --------------------------------------------------------

    latest_message = _latest_whatsapp_message(
        lead=lead,
    )

    if latest_message is None:
        return {
            "status": "skipped",
            "reason": "no_whatsapp_messages",
            "lead_id": str(
                lead_id
            ),
        }

    if (
        latest_message.direction
        != WhatsAppMessage.Direction.INBOUND
    ):
        return {
            "status": "skipped",
            "reason": (
                "latest_message_not_inbound"
            ),
            "lead_id": str(
                lead_id
            ),
            "latest_message_id": str(
                latest_message.id
            ),
        }

    source_inbound_message_id = (
        latest_message.id
    )

    # --------------------------------------------------------
    # AI ENGAGEMENT GENERATION
    # --------------------------------------------------------

    try:

        decision = (
            EngagementService().engage(
                organization=organization,
                lead=lead,
            )
        )

    except EngagementError as exc:

        provider_error = exc.__cause__

        if isinstance(
            provider_error,
            AIProviderTransientError,
        ):
            logger.warning(
                "generate_ai_engagement_response: "
                "transient provider failure for lead %s: %s",
                lead_id,
                exc,
            )

            raise task.retry(
                exc=provider_error,
                countdown=60,
            )

        logger.error(
            "generate_ai_engagement_response: "
            "permanent engagement failure for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": (
                "engagement_generation_failed"
            ),
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    except AIProviderTransientError as exc:

        raise task.retry(
            exc=exc,
            countdown=60,
        )

    except Exception as exc:

        logger.exception(
            "generate_ai_engagement_response: "
            "unexpected generation failure for lead %s",
            lead_id,
        )

        raise task.retry(
            exc=exc,
        )

    # --------------------------------------------------------
    # RE-CHECK LEAD STATE AFTER AI GENERATION
    # --------------------------------------------------------

    lead.refresh_from_db(
        fields=[
            "organization",
            "pipeline",
            "stage",
            "ai_enabled",
        ],
    )

    try:

        permission = (
            AIPermissionService().evaluate(
                organization=organization,
                lead=lead,
            )
        )

    except AIPermissionError as exc:

        logger.error(
            "generate_ai_engagement_response: "
            "permission re-check failed for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": (
                "ai_permission_recheck_failed"
            ),
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    if not permission.allowed:
        return {
            "status": "skipped",
            "reason": permission.reason,
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # RE-CHECK WHATSAPP ACCOUNT AFTER AI GENERATION
    # --------------------------------------------------------
    #
    # The account could have been disconnected while the AI
    # provider was generating the response.
    #

    account = resolve_account_for_lead(
        organization=organization,
        lead=lead,
    )

    if account is None:
        return {
            "status": "skipped",
            "reason": (
                "no_connected_whatsapp_account"
            ),
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # RE-CHECK CONVERSATION FRESHNESS
    # --------------------------------------------------------

    latest_after_generation = (
        _latest_whatsapp_message(
            lead=lead,
        )
    )

    if (
        latest_after_generation is None
        or latest_after_generation.id
        != source_inbound_message_id
        or latest_after_generation.direction
        != WhatsAppMessage.Direction.INBOUND
    ):
        return {
            "status": "skipped",
            "reason": (
                "conversation_changed_during_generation"
            ),
            "lead_id": str(
                lead_id
            ),
            "source_message_id": str(
                source_inbound_message_id
            ),
        }

    # --------------------------------------------------------
    # NO CUSTOMER-FACING ENGAGEMENT
    # --------------------------------------------------------

    if not decision.should_engage:

        try:

            with transaction.atomic():

                lead = (
                    Lead.objects
                    .select_for_update()
                    .select_related(
                        "organization",
                        "pipeline",
                        "stage",
                    )
                    .get(
                        id=lead_id,
                    )
                )

                permission = (
                    AIPermissionService().evaluate(
                        organization=organization,
                        lead=lead,
                    )
                )

                if not permission.allowed:
                    return {
                        "status": "skipped",
                        "reason": (
                            permission.reason
                        ),
                        "lead_id": str(
                            lead_id
                        ),
                    }

                latest_final = (
                    _latest_whatsapp_message(
                        lead=lead,
                    )
                )

                if (
                    latest_final is None
                    or latest_final.id
                    != source_inbound_message_id
                    or latest_final.direction
                    != WhatsAppMessage.Direction.INBOUND
                ):
                    return {
                        "status": "skipped",
                        "reason": (
                            "conversation_changed_before_finalize"
                        ),
                        "lead_id": str(
                            lead_id
                        ),
                    }

                crm_result = (
                    CRMActionExecutor().execute(
                        organization=organization,
                        lead=lead,
                        actions=decision.crm_actions,
                    )
                )

        except CRMActionExecutionError as exc:

            logger.error(
                "generate_ai_engagement_response: "
                "CRM action failed for lead %s: %s",
                lead_id,
                exc,
            )

            return {
                "status": "failed",
                "reason": "crm_action_failed",
                "lead_id": str(
                    lead_id
                ),
                "error": str(
                    exc
                ),
            }

        return {
            "status": "completed",
            "reason": "no_engagement",
            "lead_id": str(
                lead_id
            ),
            "crm": crm_result,
        }

    # --------------------------------------------------------
    # CUSTOMER-FACING MESSAGE VALIDATION
    # --------------------------------------------------------

    body = (
        decision.message.strip()
    )

    if not body:
        return {
            "status": "failed",
            "reason": (
                "empty_engagement_message"
            ),
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # FINAL ATOMIC CHECK + CRM + QUEUE
    # --------------------------------------------------------

    try:

        with transaction.atomic():

            lead = (
                Lead.objects
                .select_for_update()
                .select_related(
                    "organization",
                    "pipeline",
                    "stage",
                )
                .get(
                    id=lead_id,
                )
            )

            latest_final = (
                _latest_whatsapp_message(
                    lead=lead,
                )
            )

            if (
                latest_final is None
                or latest_final.id
                != source_inbound_message_id
                or latest_final.direction
                != WhatsAppMessage.Direction.INBOUND
            ):
                return {
                    "status": "skipped",
                    "reason": (
                        "conversation_changed_before_send"
                    ),
                    "lead_id": str(
                        lead_id
                    ),
                }

            permission = (
                AIPermissionService().evaluate(
                    organization=organization,
                    lead=lead,
                )
            )

            if not permission.allowed:
                return {
                    "status": "skipped",
                    "reason": permission.reason,
                    "lead_id": str(
                        lead_id
                    ),
                }

            # ------------------------------------------------
            # REFRESH WHATSAPP ACCOUNT INSIDE FINAL TRANSACTION
            # ------------------------------------------------
            #
            # The connection may have changed while AI was
            # generating the response.
            #

            account = resolve_account_for_lead(
                organization=organization,
                lead=lead,
            )

            if account is None:
                return {
                    "status": "skipped",
                    "reason": (
                        "no_connected_whatsapp_account"
                    ),
                    "lead_id": str(
                        lead_id
                    ),
                }

            # ------------------------------------------------
            # DUPLICATE AI RESPONSE PROTECTION
            # ------------------------------------------------

            if _has_existing_ai_response(
                lead=lead,
                inbound_message=latest_final,
                body=body,
            ):
                return {
                    "status": "skipped",
                    "reason": (
                        "duplicate_ai_response"
                    ),
                    "lead_id": str(
                        lead_id
                    ),
                    "source_message_id": str(
                        source_inbound_message_id
                    ),
                }

            # ------------------------------------------------
            # WHATSAPP SEND ELIGIBILITY
            # ------------------------------------------------

            send_eligible, eligibility_reason = (
                _whatsapp_send_eligible(
                    lead=lead,
                    inbound_message=latest_final,
                    account=account,
                )
            )

            if not send_eligible:
                logger.info(
                    "generate_ai_engagement_response: "
                    "WhatsApp send skipped for lead %s: %s",
                    lead_id,
                    eligibility_reason,
                )

                return {
                    "status": "skipped",
                    "reason": eligibility_reason,
                    "lead_id": str(
                        lead_id
                    ),
                    "source_message_id": str(
                        source_inbound_message_id
                    ),
                }

            # ------------------------------------------------
            # CRM ACTIONS
            # ------------------------------------------------

            crm_result = (
                CRMActionExecutor().execute(
                    organization=organization,
                    lead=lead,
                    actions=decision.crm_actions,
                )
            )

            # ------------------------------------------------
            # QUEUE OUTBOUND WHATSAPP MESSAGE
            # ------------------------------------------------
            #
            # The EngagementDecision currently supports one explicit
            # media selection: file_document_id. When present, queue
            # the existing organization-owned Document as a WhatsApp
            # document. Otherwise preserve the existing text path.
            #
            # The actual document ownership/activity/file validation
            # is performed again by the WhatsApp send service at send
            # time, so a stale AI decision cannot send an invalid file.
            # --------------------------------------------------------

            outbound_message_kwargs = {
                "organization": organization,
                "account": account,
                "to_number": lead.phone,
                "body": body,
                "lead": lead,
            }

            if decision.file_document_id is not None:
                try:
                    document_id = int(
                        decision.file_document_id
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "Engagement decision contains an invalid "
                        "file_document_id."
                    ) from exc

                if document_id <= 0:
                    raise ValueError(
                        "Engagement decision contains an invalid "
                        "file_document_id."
                    )

                outbound_message_kwargs.update({
                    "message_type": (
                        WhatsAppMessage.MessageType.DOCUMENT
                    ),
                    "media_payload": {
                        "source": "document",
                        "document_id": document_id,
                    },
                })

            outbound_message = (
                queue_outbound_message(
                    **outbound_message_kwargs
                )
            )

            # ------------------------------------------------
            # STORE AI METADATA FOR IDEMPOTENCY / AUDIT
            # ------------------------------------------------

            outbound_message.raw_payload = {
                "shvya_ai": {
                    "source_inbound_message_id": (
                        str(
                            source_inbound_message_id
                        )
                    ),
                    "model": decision.model,
                    "reason": decision.reason,
                }
            }

            outbound_message.save(
                update_fields=[
                    "raw_payload",
                    "updated_at",
                ],
            )

            # ------------------------------------------------
            # DISPATCH EXISTING WHATSAPP SENDER AFTER COMMIT
            # ------------------------------------------------

            transaction.on_commit(
                lambda message_id=outbound_message.id: (
                    send_whatsapp_message_task.delay(
                        str(
                            message_id
                        )
                    )
                )
            )

    except (
        CRMActionExecutionError,
        AIPermissionError,
    ) as exc:

        logger.error(
            "generate_ai_engagement_response: "
            "finalization failed for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": "finalization_failed",
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    except Exception as exc:

        logger.exception(
            "generate_ai_engagement_response: "
            "unexpected finalization failure for lead %s",
            lead_id,
        )

        raise task.retry(
            exc=exc,
        )

    return {
        "status": "completed",
        "lead_id": str(
            lead_id
        ),
        "engaged": True,
        "crm": crm_result,
        "message_id": str(
            outbound_message.id
        ),
        "source_message_id": str(
            source_inbound_message_id
        ),
        "model": decision.model,
    }

# ============================================================
# CANONICAL AI ENGAGEMENT TASK
# ============================================================


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.generate_ai_engagement_response",
)
def generate_ai_engagement_response(
    self,
    lead_id: str,
):
    """
    Canonical production AI Engagement worker.

    Celery payload contains Lead ID only.
    """
    return _execute_ai_engagement_response(
        task=self,
        lead_id=lead_id,
    )


# ============================================================
# KNOWLEDGE INGESTION — UPLOADED DOCUMENT
# ============================================================


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.ingest_and_index_document",
)
def ingest_and_index_document(
    self,
    document_id: int,
    organization_id: int,
):
    """
    Extract, chunk, and embed one uploaded knowledge Document.

    Runs off the request thread: file parsing (PDF/DOCX/XLSX) and
    OpenAI embedding calls are both too slow for a request/response
    cycle.

    Extraction failures are permanent (bad file, unsupported
    content) and are NOT retried — KnowledgeIngestionService has
    already persisted FAILED + processing_error on the Document.

    Embedding failures mark the new Document version as FAILED and
    INACTIVE. The previously published version remains active and
    can continue serving retrieval while embeddings can be retried
    later via reindex_document_embeddings.
    """

    from apps.ai_engagement.models import Document
    from apps.ai_engagement.services.embedding_index import (
        EmbeddingIndexError,
        EmbeddingIndexService,
    )
    from apps.ai_engagement.services.knowledge import (
        KnowledgeExtractionError,
        KnowledgeIngestionService,
    )

    try:
        document = (
            Document.objects
            .select_related(
                "organization",
            )
            .get(
                id=document_id,
                organization_id=organization_id,
            )
        )

    except Document.DoesNotExist:

        logger.warning(
            "ingest_and_index_document: "
            "document %s not found",
            document_id,
        )

        return {
            "status": "skipped",
            "reason": "document_not_found",
            "document_id": document_id,
        }

    try:
        chunk_count = (
            KnowledgeIngestionService().ingest_document(
                document
            )
        )

    except KnowledgeExtractionError as exc:

        logger.error(
            "ingest_and_index_document: "
            "extraction failed for document %s: %s",
            document_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": "extraction_failed",
            "document_id": document_id,
            "error": str(exc),
        }

    except Exception as exc:

        logger.exception(
            "ingest_and_index_document: "
            "unexpected extraction failure for document %s",
            document_id,
        )

        raise self.retry(
            exc=exc,
        )

    try:
        indexed_count = (
            EmbeddingIndexService().index_document(
                document
            )
        )

        document = (
            KnowledgeIngestionService().publish_document_version(
                document,
            )
        )

    except EmbeddingIndexError as exc:

        logger.error(
            "ingest_and_index_document: "
            "embedding failed for document %s: %s",
            document_id,
            exc,
        )

        document.processing_status = (
            Document.ProcessingStatus.FAILED
        )
        document.processing_error = str(exc)
        document.is_active = False
        document.save(
            update_fields=[
                "processing_status",
                "processing_error",
                "is_active",
                "updated_at",
            ]
        )

        return {
            "status": "failed",
            "reason": "embedding_failed",
            "document_id": document_id,
            "chunk_count": chunk_count,
            "error": str(exc),
        }

    except Exception as exc:

        logger.exception(
            "ingest_and_index_document: "
            "unexpected embedding failure for document %s",
            document_id,
        )

        raise self.retry(
            exc=exc,
        )

    logger.info(
        "ingest_and_index_document: "
        "indexed %s/%s chunks for document %s",
        indexed_count,
        chunk_count,
        document_id,
    )

    return {
        "status": "completed",
        "document_id": document_id,
        "chunk_count": chunk_count,
        "indexed_count": indexed_count,
    }


# ============================================================
# KNOWLEDGE INGESTION — URL SOURCE
# ============================================================


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.ingest_and_index_url_source",
)
def ingest_and_index_url_source(
    self,
    source_id: int,
    organization_id: int,
):
    """
    Fetch, chunk, and embed one URL KnowledgeSource.

    ingest_url() creates the new Document version as COMPLETED but
    INACTIVE. The worker resolves that newest completed version,
    indexes its embeddings, and only then publishes it.
    """

    from apps.ai_engagement.models import (
        Document,
        KnowledgeSource,
    )
    from apps.ai_engagement.services.embedding_index import (
        EmbeddingIndexError,
        EmbeddingIndexService,
    )
    from apps.ai_engagement.services.knowledge import (
        KnowledgeExtractionError,
        KnowledgeIngestionService,
    )

    try:
        source = (
            KnowledgeSource.objects
            .select_related(
                "organization",
            )
            .get(
                id=source_id,
                organization_id=organization_id,
            )
        )

    except KnowledgeSource.DoesNotExist:

        logger.warning(
            "ingest_and_index_url_source: "
            "source %s not found",
            source_id,
        )

        return {
            "status": "skipped",
            "reason": "source_not_found",
            "source_id": source_id,
        }

    ingestion_service = KnowledgeIngestionService()

    try:
        chunk_count = (
            ingestion_service.ingest_url(
                source
            )
        )

    except KnowledgeExtractionError as exc:

        logger.error(
            "ingest_and_index_url_source: "
            "extraction failed for source %s: %s",
            source_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": "extraction_failed",
            "source_id": source_id,
            "error": str(exc),
        }

    except Exception as exc:

        logger.exception(
            "ingest_and_index_url_source: "
            "unexpected extraction failure for source %s",
            source_id,
        )

        raise self.retry(
            exc=exc,
        )

    source_key = (
        ingestion_service.normalize_url(
            source.url
        )
    )

    document = (
        Document.objects
        .filter(
            organization=source.organization,
            source_key=source_key,
            processing_status=(
                Document.ProcessingStatus.COMPLETED
            ),
        )
        .order_by(
            "-version",
            "-id",
        )
        .first()
    )

    if document is None:

        logger.error(
            "ingest_and_index_url_source: "
            "no completed document found after ingest "
            "for source %s",
            source_id,
        )

        return {
            "status": "failed",
            "reason": "document_not_found_after_ingest",
            "source_id": source_id,
        }

    try:
        indexed_count = (
            EmbeddingIndexService().index_document(
                document
            )
        )

        document = (
            ingestion_service.publish_document_version(
                document,
            )
        )

    except EmbeddingIndexError as exc:

        logger.error(
            "ingest_and_index_url_source: "
            "embedding failed for source %s: %s",
            source_id,
            exc,
        )

        document.processing_status = (
            Document.ProcessingStatus.FAILED
        )
        document.processing_error = str(exc)
        document.is_active = False
        document.save(
            update_fields=[
                "processing_status",
                "processing_error",
                "is_active",
                "updated_at",
            ]
        )

        return {
            "status": "failed",
            "reason": "embedding_failed",
            "source_id": source_id,
            "document_id": document.id,
            "chunk_count": chunk_count,
            "error": str(exc),
        }

    except Exception as exc:

        logger.exception(
            "ingest_and_index_url_source: "
            "unexpected embedding failure for source %s",
            source_id,
        )

        raise self.retry(
            exc=exc,
        )

    logger.info(
        "ingest_and_index_url_source: "
        "indexed %s/%s chunks for source %s (document %s)",
        indexed_count,
        chunk_count,
        source_id,
        document.id,
    )

    return {
        "status": "completed",
        "source_id": source_id,
        "document_id": document.id,
        "chunk_count": chunk_count,
        "indexed_count": indexed_count,
    }


# ============================================================
# KNOWLEDGE REINDEX — EMBEDDINGS ONLY
# ============================================================


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.reindex_document_embeddings",
)
def reindex_document_embeddings(
    self,
    document_id: int,
    organization_id: int,
):
    """
    Re-generate embeddings for an already-extracted Document
    without re-parsing the source file/URL.

    Used after an embedding failure, or after switching
    OPENAI_EMBEDDING_MODEL.
    """

    from apps.ai_engagement.models import Document
    from apps.ai_engagement.services.embedding_index import (
        EmbeddingIndexError,
        EmbeddingIndexService,
    )

    try:
        document = Document.objects.get(
            id=document_id,
            organization_id=organization_id,
        )

    except Document.DoesNotExist:

        logger.warning(
            "reindex_document_embeddings: "
            "document %s not found",
            document_id,
        )

        return {
            "status": "skipped",
            "reason": "document_not_found",
            "document_id": document_id,
        }

    try:
        indexed_count = (
            EmbeddingIndexService().index_document(
                document,
                only_missing=False,
            )
        )

    except EmbeddingIndexError as exc:

        logger.error(
            "reindex_document_embeddings: "
            "embedding failed for document %s: %s",
            document_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": "embedding_failed",
            "document_id": document_id,
            "error": str(exc),
        }

    except Exception as exc:

        logger.exception(
            "reindex_document_embeddings: "
            "unexpected embedding failure for document %s",
            document_id,
        )

        raise self.retry(
            exc=exc,
        )

    return {
        "status": "completed",
        "document_id": document_id,
        "indexed_count": indexed_count,
    }