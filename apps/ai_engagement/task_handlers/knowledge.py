"""Knowledge ingestion and embedding task boundaries."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


def _transient_index_failure(exc):
    """Use provider error types/codes, never guess retryability from prose."""
    from openai import APIConnectionError, APIStatusError

    current = exc
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, (APIConnectionError, TimeoutError, ConnectionError)):
            return True
        if isinstance(current, APIStatusError):
            code = str(getattr(current, 'code', '') or '').casefold()
            if code in {'insufficient_quota', 'billing_hard_limit_reached'}:
                return False
            return current.status_code in {408, 409, 429} or current.status_code >= 500
        current = current.__cause__
    return False


def _retry_indexing(task, *, document, exc, kwargs):
    if _transient_index_failure(exc) and task.request.retries < task.max_retries:
        document.processing_error = 'Temporary embedding service failure. Retrying automatically.'
        document.save(update_fields=['processing_error', 'updated_at'])
        # Retain the extracted chunks and original document identity. A retry
        # must not fetch a newer URL version or charge again for extraction.
        raise task.retry(exc=exc, args=(), kwargs=kwargs,
                         countdown=min(30 * (2 ** task.request.retries), 120))


def _indexable_chunk_count(document):
    from apps.ai_engagement.services.embedding_index import EmbeddingIndexError

    chunks = document.chunks.filter(is_active=True)
    if chunks.exclude(organization_id=document.organization_id).exists():
        raise EmbeddingIndexError('Knowledge chunks do not match the document organization.')
    return chunks.count()


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.ingest_and_index_document",
)
def ingest_and_index_document(
    self,
    document_id: int,
    organization_id=None,
):
    """
    Extract, chunk, and embed one uploaded knowledge Document.

    organization_id is optional for backward compatibility with
    older upload callers that queued only document_id.

    When provided, organization_id is still used as an
    organization-scope guard.
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

    # ========================================================
    # RESOLVE DOCUMENT
    # ========================================================

    try:

        document = (
            Document.objects
            .select_related(
                "organization",
            )
            .get(
                id=document_id,
                **(
                    {
                        "organization_id": organization_id
                    }
                    if organization_id is not None
                    else {}
                ),
            )
        )

    except Document.DoesNotExist:

        # A task can theoretically run before the surrounding
        # transaction is visible if an old caller queues the task
        # directly instead of using transaction.on_commit().
        #
        # Retry briefly so that case does not leave the Document
        # permanently stuck in PENDING.

        if self.request.retries < self.max_retries:

            logger.warning(
                "ingest_and_index_document: "
                "document %s not found yet; retrying",
                document_id,
            )

            raise self.retry(
                countdown=5,
            )

        logger.warning(
            "ingest_and_index_document: "
            "document %s not found after retries",
            document_id,
        )

        return {
            "status": "skipped",
            "reason": "document_not_found",
            "document_id": document_id,
        }

    # ========================================================
    # EXTRACTION / CHUNKING
    # ========================================================

    try:

        # A previous extraction can already have succeeded before indexing
        # failed transiently. Reuse those rows so retries are idempotent.
        chunk_count = (_indexable_chunk_count(document)
                       if document.processing_status == Document.ProcessingStatus.COMPLETED else 0)
        if not chunk_count:
            chunk_count = KnowledgeIngestionService().ingest_document(document)

    except (KnowledgeExtractionError, EmbeddingIndexError) as exc:

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
            "unexpected extraction failure for "
            "document %s",
            document_id,
        )

        raise self.retry(
            exc=exc,
        )

    # ========================================================
    # EMBEDDING / INDEXING
    # ========================================================

    try:

        indexed_count = (
            EmbeddingIndexService().index_document(
                document
            )
        )

        document = (
            KnowledgeIngestionService()
            .publish_document_version(
                document
            )
        )

    except EmbeddingIndexError as exc:

        _retry_indexing(self, document=document, exc=exc,
                        kwargs={'document_id': document.id, 'organization_id': document.organization_id})

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
            "unexpected embedding failure for "
            "document %s",
            document_id,
        )

        raise self.retry(
            exc=exc,
        )

    # ========================================================
    # SUCCESS
    # ========================================================

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
    document_id: int | None = None,
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

    if not source.is_active:
        return {'status': 'skipped', 'reason': 'source_inactive', 'source_id': source_id}

    # A retry after extraction is bound to the exact unpublished version.
    # Re-fetching the URL here used to create another document and more chunks.
    document = None
    if document_id is not None:
        document = Document.objects.filter(pk=document_id, organization_id=organization_id,
            source_key=str(source.url).strip(),
            processing_status=Document.ProcessingStatus.COMPLETED).first()
        if document is None:
            return {'status': 'skipped', 'reason': 'retry_document_not_found', 'source_id': source_id}

    try:
        chunk_count = (_indexable_chunk_count(document) if document is not None
                       else ingestion_service.ingest_url(source))

    except (KnowledgeExtractionError, EmbeddingIndexError) as exc:

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

    source_key = document.source_key if document is not None else (
        ingestion_service.normalize_url(
            source.url
        )
    )

    document = document or (
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

        _retry_indexing(self, document=document, exc=exc,
                        kwargs={'source_id': source_id, 'organization_id': organization_id,
                                'document_id': document.pk})

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
        recovering = document.processing_status == Document.ProcessingStatus.FAILED
        chunk_count = _indexable_chunk_count(document)
        if recovering and (not document.source_key or not chunk_count):
            return {'status': 'failed', 'reason': 'no_extracted_chunks', 'document_id': document_id}
        if document.processing_status not in {Document.ProcessingStatus.COMPLETED,
                                             Document.ProcessingStatus.FAILED}:
            return {'status': 'skipped', 'reason': 'document_not_ready', 'document_id': document_id}
        indexed_count = (
            EmbeddingIndexService().index_document(
                document,
                only_missing=False,
            )
        )
        if recovering:
            from apps.ai_engagement.services.knowledge import KnowledgeIngestionService
            KnowledgeIngestionService().publish_document_version(document, recover_failed=True)

    except EmbeddingIndexError as exc:

        _retry_indexing(self, document=document, exc=exc,
                        kwargs={'document_id': document.id, 'organization_id': organization_id})

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
