from __future__ import annotations

from collections.abc import Iterable

from apps.ai_engagement.models import Chunk
from apps.ai_engagement.services.embeddings import (
    EmbeddingError,
    EmbeddingService,
)


class EmbeddingIndexError(Exception):
    """
    Raised when a knowledge chunk cannot be embedded/indexed.
    """


class EmbeddingIndexService:
    """
    Coordinates embedding generation and persistence.

    Flow:

        Chunk
          ↓
        EmbeddingService
          ↓
        embedding vector
          ↓
        Chunk.embedding
          ↓
        PostgreSQL / pgvector
    """

    def __init__(
        self,
        *,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self.embedding_service = (
            embedding_service
            or EmbeddingService()
        )

    # ============================================================
    # SINGLE CHUNK
    # ============================================================

    def index_chunk(
        self,
        chunk: Chunk,
    ) -> Chunk:
        """
        Generate and persist an embedding for one chunk.
        """

        if not chunk.is_active:
            raise EmbeddingIndexError(
                f"Chunk {chunk.pk} is inactive."
            )

        content = (
            chunk.content or ""
        ).strip()

        if not content:
            raise EmbeddingIndexError(
                f"Chunk {chunk.pk} has empty content."
            )

        try:
            vector = (
                self.embedding_service.embed_text(
                    content
                )
            )
        except EmbeddingError as exc:
            raise EmbeddingIndexError(
                f"Unable to generate embedding "
                f"for chunk {chunk.pk}: {exc}"
            ) from exc

        expected_dimensions = (
            Chunk.EMBEDDING_DIMENSIONS
        )

        actual_dimensions = len(
            vector
        )

        if actual_dimensions != expected_dimensions:
            raise EmbeddingIndexError(
                "Embedding dimension mismatch. "
                f"Expected {expected_dimensions}, "
                f"received {actual_dimensions}."
            )

        chunk.embedding = vector

        chunk.save(
            update_fields=[
                "embedding",
                "updated_at",
            ]
        )

        return chunk

    # ============================================================
    # MULTIPLE CHUNKS
    # ============================================================

    def index_chunks(
        self,
        chunks: Iterable[Chunk],
    ) -> int:
        """
        Index multiple chunks.

        Returns:
            Number of successfully indexed chunks.

        Notes:
            Embeddings are generated in batches by the provider
            where possible. Persistence remains scoped to individual
            chunk records so a provider failure does not mark
            unrelated chunks as complete.
        """

        chunk_list = list(
            chunks
        )

        if not chunk_list:
            return 0

        for chunk in chunk_list:

            if not chunk.is_active:
                raise EmbeddingIndexError(
                    f"Chunk {chunk.pk} is inactive."
                )

            if not (
                chunk.content or ""
            ).strip():
                raise EmbeddingIndexError(
                    f"Chunk {chunk.pk} has empty content."
                )

        try:
            vectors = (
                self.embedding_service.embed_texts(
                    [
                        chunk.content.strip()
                        for chunk in chunk_list
                    ]
                )
            )
        except EmbeddingError as exc:
            raise EmbeddingIndexError(
                f"Unable to generate batch embeddings: {exc}"
            ) from exc

        if len(vectors) != len(
            chunk_list
        ):
            raise EmbeddingIndexError(
                "Embedding provider returned an unexpected "
                "number of vectors."
            )

        expected_dimensions = (
            Chunk.EMBEDDING_DIMENSIONS
        )

        for index, vector in enumerate(
            vectors
        ):

            if len(vector) != (
                expected_dimensions
            ):
                raise EmbeddingIndexError(
                    "Embedding dimension mismatch for "
                    f"chunk {chunk_list[index].pk}. "
                    f"Expected {expected_dimensions}, "
                    f"received {len(vector)}."
                )

        for chunk, vector in zip(
            chunk_list,
            vectors,
            strict=True,
        ):

            chunk.embedding = vector

        Chunk.objects.bulk_update(
            chunk_list,
            [
                "embedding",
                "updated_at",
            ],
        )

        return len(
            chunk_list
        )

    # ============================================================
    # DOCUMENT
    # ============================================================

    def index_document(
        self,
        document,
        *,
        only_missing: bool = True,
    ) -> int:
        """
        Index active chunks belonging to one document.
        """

        queryset = (
            document.chunks
            .filter(
                is_active=True,
            )
            .order_by(
                "chunk_index",
            )
        )

        if only_missing:
            queryset = queryset.filter(
                embedding__isnull=True,
            )

        chunks = list(
            queryset
        )

        return self.index_chunks(
            chunks
        )

    # ============================================================
    # ORGANIZATION
    # ============================================================

    def index_organization(
        self,
        organization,
    ) -> int:
        """
        Index all active, currently unembedded chunks for one
        organization.
        """

        chunks = list(
            Chunk.objects
            .filter(
                organization=organization,
                is_active=True,
                embedding__isnull=True,
            )
            .select_related(
                "document",
            )
            .order_by(
                "document_id",
                "chunk_index",
            )
        )

        return self.index_chunks(
            chunks
        )