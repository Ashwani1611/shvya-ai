from __future__ import annotations

from typing import Sequence

from django.conf import settings
from openai import OpenAI

from apps.ai_engagement.services.credits import (
    AICreditError,
    AICreditService,
    AICreditUnavailableError,
)


class EmbeddingError(Exception):
    """
    Raised when an embedding cannot be generated.
    """


class EmbeddingService:
    """
    Provider-isolated embedding service.

    The rest of SHVYA should call this service rather than
    importing the OpenAI SDK directly.

    V1 provider:
        OpenAI

    V1 model:
        text-embedding-3-small

    Organization-scoped calls may supply ``organization_id``. Those calls are
    reserved and settled against the same manual AI-credit wallet used by text
    generation. This is important for semantic retrieval: a zero-credit
    organization must not be able to consume an embedding call before the text
    provider later rejects the request.
    """

    DEFAULT_MODEL = "text-embedding-3-small"

    DEFAULT_DIMENSIONS = 1536

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:

        self.api_key = (
            api_key
            or getattr(
                settings,
                "OPENAI_API_KEY",
                "",
            )
            or ""
        ).strip()

        self.model = (
            model
            or getattr(
                settings,
                "OPENAI_EMBEDDING_MODEL",
                self.DEFAULT_MODEL,
            )
            or self.DEFAULT_MODEL
        ).strip()

        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        """
        Resolve the provider client only when a real embedding call
        is made.

        This keeps service construction side-effect free and allows
        tests to mock the embedding boundary without requiring live
        provider credentials.
        """

        if not self.api_key:
            raise EmbeddingError(
                "OPENAI_API_KEY is not configured."
            )

        if not self.model:
            raise EmbeddingError(
                "OPENAI_EMBEDDING_MODEL is not configured."
            )

        if self._client is None:
            self._client = OpenAI(
                api_key=self.api_key,
            )

        return self._client

    @staticmethod
    def _response_input_tokens(response, *, fallback_texts: Sequence[str]) -> int:
        usage = getattr(response, "usage", None)
        if usage is not None:
            if isinstance(usage, dict):
                value = usage.get("input_tokens") or usage.get("prompt_tokens")
            else:
                value = (
                    getattr(usage, "input_tokens", None)
                    or getattr(usage, "prompt_tokens", None)
                )
            if value is not None:
                try:
                    return max(int(value), 0)
                except (TypeError, ValueError):
                    pass

        return sum(
            AICreditService.estimate_tokens(text)
            for text in fallback_texts
        )

    def _reserve_credits(
        self,
        *,
        organization_id,
        texts: Sequence[str],
        feature: str,
        reference_id: str,
    ):
        if not organization_id:
            return None
        try:
            return AICreditService.reserve_embedding(
                organization_id=organization_id,
                model=self.model,
                texts=list(texts),
                feature=feature,
                reference_id=reference_id,
            )
        except AICreditUnavailableError as exc:
            raise EmbeddingError(str(exc)) from exc
        except AICreditError as exc:
            raise EmbeddingError(
                f"Unable to reserve organization AI credits for embedding: {exc}"
            ) from exc

    @staticmethod
    def _release_credits(reservation) -> None:
        if reservation is None:
            return
        try:
            AICreditService.release(reservation)
        except AICreditError:
            # Preserve the original provider error. An active reservation is
            # visible to Superadmin if release itself requires investigation.
            pass

    def _settle_credits(
        self,
        reservation,
        *,
        response,
        texts: Sequence[str],
    ) -> None:
        if reservation is None:
            return
        input_tokens = self._response_input_tokens(
            response,
            fallback_texts=texts,
        )
        try:
            AICreditService.settle(
                reservation=reservation,
                input_tokens=input_tokens,
                output_tokens=0,
                embedding=True,
                metadata={
                    "provider": "openai",
                    "operation": "embedding",
                },
            )
        except AICreditError as exc:
            raise EmbeddingError(
                f"Embedding completed but AI-credit settlement failed: {exc}"
            ) from exc

    def embed_text(
        self,
        text: str,
        *,
        organization_id=None,
        feature: str = "embedding",
        reference_id: str = "",
    ) -> list[float]:
        """
        Generate one embedding vector.

        Supplying ``organization_id`` enables organization AI-credit metering.
        """

        normalized = (
            text or ""
        ).strip()

        if not normalized:
            raise EmbeddingError(
                "Cannot generate an embedding for empty text."
            )

        reservation = self._reserve_credits(
            organization_id=organization_id,
            texts=[normalized],
            feature=feature,
            reference_id=str(reference_id or "")[:150],
        )

        try:

            response = (
                self._get_client().embeddings.create(
                    model=self.model,
                    input=normalized,
                )
            )

        except EmbeddingError:
            self._release_credits(reservation)
            raise

        except Exception as exc:
            self._release_credits(reservation)
            raise EmbeddingError(
                f"Embedding generation failed: {exc}"
            ) from exc

        if not response.data:
            self._release_credits(reservation)
            raise EmbeddingError(
                "Embedding provider returned no data."
            )

        embedding = (
            response.data[0].embedding
        )

        if not embedding:
            self._release_credits(reservation)
            raise EmbeddingError(
                "Embedding provider returned an empty vector."
            )

        vector = list(
            embedding
        )

        if len(vector) != self.DEFAULT_DIMENSIONS:
            self._release_credits(reservation)
            raise EmbeddingError(
                "Embedding provider returned an unexpected "
                f"vector dimension: {len(vector)}. "
                f"Expected {self.DEFAULT_DIMENSIONS}."
            )

        self._settle_credits(
            reservation,
            response=response,
            texts=[normalized],
        )

        return vector

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        organization_id=None,
        feature: str = "embedding",
        reference_id: str = "",
    ) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Empty entries are rejected rather than silently producing
        invalid knowledge records. Supplying ``organization_id`` enables
        organization AI-credit metering for the whole provider batch.
        """

        normalized_texts = [
            (text or "").strip()
            for text in texts
        ]

        if not normalized_texts:
            return []

        if any(
            not text
            for text in normalized_texts
        ):
            raise EmbeddingError(
                "Cannot generate embeddings for empty text."
            )

        reservation = self._reserve_credits(
            organization_id=organization_id,
            texts=normalized_texts,
            feature=feature,
            reference_id=str(reference_id or "")[:150],
        )

        try:

            response = (
                self._get_client().embeddings.create(
                    model=self.model,
                    input=normalized_texts,
                )
            )

        except EmbeddingError:
            self._release_credits(reservation)
            raise

        except Exception as exc:
            self._release_credits(reservation)
            raise EmbeddingError(
                f"Batch embedding generation failed: {exc}"
            ) from exc

        if len(response.data) != len(
            normalized_texts
        ):
            self._release_credits(reservation)
            raise EmbeddingError(
                "Embedding provider returned an unexpected "
                "number of vectors."
            )

        ordered = sorted(
            response.data,
            key=lambda item: item.index,
        )

        embeddings = [
            list(
                item.embedding
            )
            for item in ordered
        ]

        if any(
            not embedding
            for embedding in embeddings
        ):
            self._release_credits(reservation)
            raise EmbeddingError(
                "Embedding provider returned an empty vector."
            )

        if any(
            len(embedding) != self.DEFAULT_DIMENSIONS
            for embedding in embeddings
        ):
            self._release_credits(reservation)
            raise EmbeddingError(
                "Embedding provider returned an unexpected "
                f"vector dimension. Expected "
                f"{self.DEFAULT_DIMENSIONS}."
            )

        self._settle_credits(
            reservation,
            response=response,
            texts=normalized_texts,
        )

        return embeddings
