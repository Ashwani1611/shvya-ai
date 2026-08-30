from __future__ import annotations

from dataclasses import dataclass

from django.db.models import QuerySet

from pgvector.django import CosineDistance

from apps.ai_engagement.models import Chunk


class RetrievalError(Exception):
    """
    Raised when knowledge retrieval cannot be completed.
    """


@dataclass(frozen=True)
class RetrievedChunk:
    """
    Lightweight representation of a retrieved knowledge chunk.
    """

    chunk: Chunk
    distance: float

    @property
    def similarity(self) -> float:
        """
        Convert cosine distance into a simple similarity score.

        cosine similarity = 1 - cosine distance
        """

        return 1.0 - self.distance


class KnowledgeRetrievalService:
    """
    Production retrieval service for SHVYA knowledge.

    Retrieval flow:

        query vector
            ↓
        organization scope
            ↓
        active documents
            ↓
        active chunks
            ↓
        pgvector cosine distance
            ↓
        top-K chunks

    This service does not generate embeddings itself.

    Query embeddings will later be provided by EmbeddingService.
    """

    DEFAULT_LIMIT = 5

    MAX_LIMIT = 20

    # ============================================================
    # VECTOR RETRIEVAL
    # ============================================================

    def retrieve_by_vector(
        self,
        *,
        organization,
        query_vector: list[float],
        limit: int = DEFAULT_LIMIT,
    ) -> list[RetrievedChunk]:
        """
        Retrieve the most relevant active knowledge chunks for
        a supplied embedding vector.

        This method does not call any external AI provider and
        can therefore be tested without an API key.
        """

        self._validate_vector(
            query_vector
        )

        limit = self._validate_limit(
            limit
        )

        queryset: QuerySet[Chunk] = (
            Chunk.objects
            .filter(
                organization=organization,
                is_active=True,
                document__organization=organization,
                document__is_active=True,
                document__processing_status=(
                    "completed"
                ),
                embedding__isnull=False,
            )
            .annotate(
                distance=CosineDistance(
                    "embedding",
                    query_vector,
                )
            )
            .select_related(
                "document",
            )
            .order_by(
                "distance",
                "document_id",
                "chunk_index",
            )[:limit]
        )

        return [
            RetrievedChunk(
                chunk=chunk,
                distance=float(
                    chunk.distance
                ),
            )
            for chunk in queryset
        ]

    # ============================================================
    # VALIDATION
    # ============================================================

    def _validate_vector(
        self,
        query_vector: list[float],
    ) -> None:
        """
        Validate the query vector before sending it to pgvector.
        """

        if not query_vector:
            raise RetrievalError(
                "Query vector cannot be empty."
            )

        expected_dimensions = (
            Chunk.EMBEDDING_DIMENSIONS
        )

        actual_dimensions = len(
            query_vector
        )

        if actual_dimensions != (
            expected_dimensions
        ):
            raise RetrievalError(
                "Query vector dimension mismatch. "
                f"Expected {expected_dimensions}, "
                f"received {actual_dimensions}."
            )

        for value in query_vector:

            if not isinstance(
                value,
                (int, float),
            ):
                raise RetrievalError(
                    "Query vector must contain only numeric values."
                )

    def _validate_limit(
        self,
        limit: int,
    ) -> int:
        """
        Validate the requested number of results.
        """

        if limit <= 0:
            raise RetrievalError(
                "Retrieval limit must be greater than zero."
            )

        if limit > self.MAX_LIMIT:
            raise RetrievalError(
                f"Retrieval limit cannot exceed "
                f"{self.MAX_LIMIT}."
            )

        return limit