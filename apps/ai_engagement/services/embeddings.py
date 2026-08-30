from __future__ import annotations

import os
from typing import Sequence

from openai import OpenAI


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
            or os.getenv("OPENAI_API_KEY")
            or ""
        ).strip()

        self.model = (
            model
            or os.getenv(
                "OPENAI_EMBEDDING_MODEL"
            )
            or self.DEFAULT_MODEL
        ).strip()

        if not self.api_key:
            raise EmbeddingError(
                "OPENAI_API_KEY is not configured."
            )

        self.client = OpenAI(
            api_key=self.api_key
        )

    def embed_text(
        self,
        text: str,
    ) -> list[float]:
        """
        Generate one embedding vector.
        """

        normalized = (
            text or ""
        ).strip()

        if not normalized:
            raise EmbeddingError(
                "Cannot generate an embedding for empty text."
            )

        try:

            response = self.client.embeddings.create(
                model=self.model,
                input=normalized,
            )

        except Exception as exc:

            raise EmbeddingError(
                f"Embedding generation failed: {exc}"
            ) from exc

        if not response.data:
            raise EmbeddingError(
                "Embedding provider returned no data."
            )

        embedding = response.data[0].embedding

        if not embedding:
            raise EmbeddingError(
                "Embedding provider returned an empty vector."
            )

        return list(
            embedding
        )

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Empty entries are rejected rather than silently producing
        invalid knowledge records.
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

        try:

            response = self.client.embeddings.create(
                model=self.model,
                input=normalized_texts,
            )

        except Exception as exc:

            raise EmbeddingError(
                f"Batch embedding generation failed: {exc}"
            ) from exc

        if len(response.data) != len(
            normalized_texts
        ):
            raise EmbeddingError(
                "Embedding provider returned an unexpected "
                "number of vectors."
            )

        ordered = sorted(
            response.data,
            key=lambda item: item.index,
        )

        embeddings = [
            list(item.embedding)
            for item in ordered
        ]

        if any(
            not embedding
            for embedding in embeddings
        ):
            raise EmbeddingError(
                "Embedding provider returned an empty vector."
            )

        return embeddings