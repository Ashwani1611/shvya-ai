from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

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


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.process_whatsapp_engagement",
)
def process_whatsapp_engagement(
    self,
    lead_id: str,
    account_id: str,
):
    """
    Process one inbound WhatsApp Lead through AI Engagement.

    Responsibilities:
        - resolve Lead and WhatsAppAccount
        - enforce organization / AI gates
        - ask EngagementService for a decision
        - execute validated CRM action requests
        - queue a customer-facing WhatsApp response

    This task does not call Meta directly.
    """

    from apps.ai_engagement.services.crm_executor import (
        CRMActionExecutionError,
        CRMActionExecutor,
    )
    from apps.ai_engagement.services.engagement import (
        EngagementError,
        EngagementService,
    )
    from apps.channels.models import WhatsAppAccount
    from apps.channels.tasks import send_whatsapp_message_task
    from apps.crm.models import Lead
    from apps.crm.models import OrgInfo
    from services.channels.whatsapp_service import (
        queue_outbound_message,
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
            .get(id=lead_id)
        )
    except Lead.DoesNotExist:
        logger.warning(
            "process_whatsapp_engagement: "
            "lead %s not found",
            lead_id,
        )
        return {
            "status": "skipped",
            "reason": "lead_not_found",
            "lead_id": str(lead_id),
        }

    # --------------------------------------------------------
    # RESOLVE ACCOUNT
    # --------------------------------------------------------

    try:
        account = (
            WhatsAppAccount.objects
            .select_related("organization")
            .get(id=account_id)
        )
    except WhatsAppAccount.DoesNotExist:
        logger.warning(
            "process_whatsapp_engagement: "
            "account %s not found",
            account_id,
        )
        return {
            "status": "skipped",
            "reason": "account_not_found",
            "account_id": str(account_id),
        }

    # --------------------------------------------------------
    # ORGANIZATION ISOLATION
    # --------------------------------------------------------

    if account.organization_id != lead.organization_id:
        logger.error(
            "process_whatsapp_engagement: "
            "organization mismatch for lead %s and account %s",
            lead_id,
            account_id,
        )
        return {
            "status": "skipped",
            "reason": "organization_mismatch",
            "lead_id": str(lead_id),
            "account_id": str(account_id),
        }

    organization = lead.organization

    # --------------------------------------------------------
    # AI GATES
    # --------------------------------------------------------

    org_info, _created = OrgInfo.objects.get_or_create(
        organization=organization,
    )

    if not org_info.ai_enabled:
        return {
            "status": "skipped",
            "reason": "organization_ai_disabled",
            "lead_id": str(lead_id),
        }

    if not lead.ai_enabled:
        return {
            "status": "skipped",
            "reason": "lead_ai_disabled",
            "lead_id": str(lead_id),
        }

    if lead.stage_id and not lead.stage.ai_on:
        return {
            "status": "skipped",
            "reason": "stage_ai_disabled",
            "lead_id": str(lead_id),
            "stage_id": str(lead.stage_id),
        }

    # --------------------------------------------------------
    # AI ENGAGEMENT DECISION
    # --------------------------------------------------------

    try:
        decision = EngagementService().engage(
            organization=organization,
            lead=lead,
        )
    except EngagementError as exc:
        logger.error(
            "process_whatsapp_engagement: "
            "engagement failed for lead %s: %s",
            lead_id,
            exc,
        )
        return {
            "status": "failed",
            "reason": "engagement_failed",
            "lead_id": str(lead_id),
            "error": str(exc),
        }

    # --------------------------------------------------------
    # EXECUTE CRM REQUESTS
    # --------------------------------------------------------

    try:
        with transaction.atomic():
            crm_result = CRMActionExecutor().execute(
                organization=organization,
                lead=lead,
                actions=decision.crm_actions,
            )

            if not decision.should_engage:
                return {
                    "status": "completed",
                    "reason": "no_engagement",
                    "lead_id": str(lead_id),
                    "crm": crm_result,
                }

            outbound_message = queue_outbound_message(
                organization=organization,
                account=account,
                to_number=lead.phone,
                body=decision.message,
                lead=lead,
            )

            transaction.on_commit(
                lambda message_id=outbound_message.id: (
                    send_whatsapp_message_task.delay(
                        str(message_id)
                    )
                )
            )

    except CRMActionExecutionError as exc:
        logger.error(
            "process_whatsapp_engagement: "
            "CRM execution failed for lead %s: %s",
            lead_id,
            exc,
        )
        return {
            "status": "failed",
            "reason": "crm_action_failed",
            "lead_id": str(lead_id),
            "error": str(exc),
        }

    return {
        "status": "completed",
        "lead_id": str(lead_id),
        "engaged": True,
        "crm": crm_result,
        "message_id": str(outbound_message.id),
    }

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