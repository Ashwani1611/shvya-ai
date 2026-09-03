from __future__ import annotations

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


class KnowledgePipelineError(Exception):
    """
    Raised when the complete knowledge ingestion/indexing pipeline
    cannot finish successfully.
    """


class KnowledgePipelineService:
    """
    Orchestrates the existing knowledge layers.

    Responsibilities:

        KnowledgeIngestionService
            → extraction / cleaning / chunking / versioning

        EmbeddingIndexService
            → embedding generation / persistence

        KnowledgeIngestionService
            → publication of the successfully indexed version

    Retrieval remains the responsibility of
    KnowledgeRetrievalService.

    This service only coordinates the existing layers. It does not
    duplicate their implementation.
    """

    def __init__(
        self,
        *,
        ingestion_service: KnowledgeIngestionService | None = None,
        embedding_index_service: EmbeddingIndexService | None = None,
    ) -> None:

        self.ingestion_service = (
            ingestion_service
            or KnowledgeIngestionService()
        )

        self.embedding_index_service = (
            embedding_index_service
            or EmbeddingIndexService()
        )

    # ========================================================
    # FILE DOCUMENT
    # ========================================================

    def process_document(
        self,
        *,
        document: Document,
    ) -> Document:
        """
        Process an uploaded Document completely.

        Flow:

            Document
                ↓
            extraction / cleaning / chunking
                ↓
            persisted Document + chunks
                ↓
            embedding generation
                ↓
            persisted vectors
                ↓
            publish successfully indexed version

        Returns:
            The refreshed processed and published Document.
        """

        if document is None:
            raise KnowledgePipelineError(
                "Document is required."
            )

        try:

            self.ingestion_service.ingest_document(
                document,
            )

            # Re-read after ingestion so the pipeline works with
            # the persisted document state and its newly-created
            # chunks.
            processed_document = (
                Document.objects.get(
                    pk=document.pk,
                )
            )

            # The document must be fully indexed before it is
            # allowed to become the active published version.
            self.embedding_index_service.index_document(
                processed_document,
            )

            processed_document = (
                self.ingestion_service.publish_document_version(
                    processed_document,
                )
            )

        except KnowledgeExtractionError as exc:

            raise KnowledgePipelineError(
                f"Knowledge document ingestion failed: {exc}"
            ) from exc

        except EmbeddingIndexError as exc:

            raise KnowledgePipelineError(
                f"Knowledge document indexing failed: {exc}"
            ) from exc

        except Document.DoesNotExist as exc:

            raise KnowledgePipelineError(
                "Document no longer exists after ingestion."
            ) from exc

        return processed_document

    # ========================================================
    # URL SOURCE
    # ========================================================

    def process_url(
        self,
        *,
        source: KnowledgeSource,
    ) -> Document:
        """
        Process a URL KnowledgeSource completely.

        Flow:

            KnowledgeSource
                ↓
            fetch / extract / clean / chunk
                ↓
            completed unpublished Document version
                ↓
            embedding generation
                ↓
            publish successfully indexed version

        The new URL version remains inactive until indexing
        succeeds.
        """

        if source is None:
            raise KnowledgePipelineError(
                "KnowledgeSource is required."
            )

        if source.source_type != (
            KnowledgeSource.SourceType.URL
        ):
            raise KnowledgePipelineError(
                "KnowledgeSource must be a URL source."
            )

        if not source.is_active:
            raise KnowledgePipelineError(
                "Cannot process an inactive KnowledgeSource."
            )

        try:

            self.ingestion_service.ingest_url(
                source,
            )

            normalized_url = (
                self.ingestion_service._normalize_url(
                    source.url,
                )
            )

            # ingest_url() now leaves the new version COMPLETED
            # but unpublished. Find the newest completed version
            # rather than requiring is_active=True.
            document = (
                Document.objects
                .filter(
                    organization=source.organization,
                    source_key=normalized_url,
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
                raise KnowledgePipelineError(
                    "URL ingestion completed but no completed "
                    "Document version was found."
                )

            # Do not publish before embeddings exist.
            self.embedding_index_service.index_document(
                document,
            )

            # Publication is the final step. Only a successfully
            # indexed version becomes active.
            document = (
                self.ingestion_service.publish_document_version(
                    document,
                )
            )

            return document

        except KnowledgeExtractionError as exc:

            raise KnowledgePipelineError(
                f"Knowledge URL ingestion failed: {exc}"
            ) from exc

        except EmbeddingIndexError as exc:

            raise KnowledgePipelineError(
                f"Knowledge URL indexing failed: {exc}"
            ) from exc

    # ========================================================
    # REINDEX DOCUMENT
    # ========================================================

    def reindex_document(
        self,
        *,
        document: Document,
    ) -> int:
        """
        Index an existing Document's active chunks.

        Existing embeddings are skipped by the underlying
        EmbeddingIndexService when only_missing=True.

        Reindexing does not create or publish a new document
        version.
        """

        if document is None:
            raise KnowledgePipelineError(
                "Document is required."
            )

        try:

            return self.embedding_index_service.index_document(
                document,
                only_missing=True,
            )

        except EmbeddingIndexError as exc:

            raise KnowledgePipelineError(
                f"Document reindexing failed: {exc}"
            ) from exc