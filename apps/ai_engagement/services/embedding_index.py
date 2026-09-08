from __future__ import annotations

from collections.abc import Iterable

from apps.ai_engagement.models import Chunk
from apps.ai_engagement.services.credits import (
    AICreditError,
    AICreditService,
    AICreditUnavailableError,
)
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
    Coordinates embedding generation, AI-credit metering, and persistence.

    Flow:

        Chunk
          ↓
        reserve organization AI credits
          ↓
        EmbeddingService
          ↓
        settle / release credits
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
        # Keep provider creation lazy. This lets callers construct the
        # indexing service for orchestration/tests without requiring an
        # OpenAI key until an embedding is actually requested.
        self._embedding_service = embedding_service

    @property
    def embedding_service(self) -> EmbeddingService:
        if self._embedding_service is None:
            self._embedding_service = EmbeddingService()
        return self._embedding_service

    def _uses_real_embedding_service(self) -> bool:
        """Do not make injected unit-test provider doubles require a wallet."""
        return isinstance(self.embedding_service, EmbeddingService)

    def _reserve_embedding_credits(self, *, organization_id, texts, reference_id=""):
        if not self._uses_real_embedding_service():
            return None
        model = getattr(
            self.embedding_service,
            "model",
            EmbeddingService.DEFAULT_MODEL,
        )
        try:
            return AICreditService.reserve_embedding(
                organization_id=organization_id,
                model=model,
                texts=list(texts),
                feature="knowledge_embedding",
                reference_id=str(reference_id or ""),
            )
        except AICreditUnavailableError as exc:
            raise EmbeddingIndexError(str(exc)) from exc
        except AICreditError as exc:
            raise EmbeddingIndexError(
                f"Unable to reserve AI credits for embeddings: {exc}"
            ) from exc

    def _settle_embedding_credits(self, reservation, *, texts) -> None:
        if reservation is None:
            return
        estimated_tokens = sum(
            AICreditService.estimate_tokens(text)
            for text in texts
        )
        try:
            AICreditService.settle(
                reservation=reservation,
                input_tokens=estimated_tokens,
                output_tokens=0,
                embedding=True,
                metadata={"provider": "openai", "operation": "embedding"},
            )
        except AICreditError as exc:
            raise EmbeddingIndexError(
                f"Embedding completed but AI-credit settlement failed: {exc}"
            ) from exc

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

        reservation = self._reserve_embedding_credits(
            organization_id=chunk.organization_id,
            texts=[content],
            reference_id=chunk.pk,
        )

        try:
            vector = (
                self.embedding_service.embed_text(
                    content
                )
            )
        except EmbeddingError as exc:
            if reservation is not None:
                AICreditService.release(reservation)
            raise EmbeddingIndexError(
                f"Unable to generate embedding "
                f"for chunk {chunk.pk}: {exc}"
            ) from exc
        except Exception:
            if reservation is not None:
                AICreditService.release(reservation)
            raise

        self._settle_embedding_credits(
            reservation,
            texts=[content],
        )

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

        organization_ids = {chunk.organization_id for chunk in chunk_list}
        if self._uses_real_embedding_service() and len(organization_ids) != 1:
            raise EmbeddingIndexError(
                "A metered embedding batch cannot contain multiple organizations."
            )

        normalized_texts = [
            chunk.content.strip()
            for chunk in chunk_list
        ]
        reservation = self._reserve_embedding_credits(
            organization_id=chunk_list[0].organization_id,
            texts=normalized_texts,
            reference_id=chunk_list[0].document_id,
        )

        try:
            vectors = (
                self.embedding_service.embed_texts(
                    normalized_texts
                )
            )
        except EmbeddingError as exc:
            if reservation is not None:
                AICreditService.release(reservation)
            raise EmbeddingIndexError(
                f"Unable to generate batch embeddings: {exc}"
            ) from exc
        except Exception:
            if reservation is not None:
                AICreditService.release(reservation)
            raise

        self._settle_embedding_credits(
            reservation,
            texts=normalized_texts,
        )

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
