from __future__ import annotations

import math
import re
from dataclasses import dataclass

from django.db.models import Q, QuerySet

from pgvector.django import CosineDistance

from apps.ai_engagement.models import Chunk


class RetrievalError(Exception):
    """Raised when knowledge retrieval cannot be completed."""


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+.-]*", flags=re.IGNORECASE)


def _normalized_text(value) -> str:
    return " ".join(str(value or "").casefold().split())


def _tokens(value) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for token in _WORD_RE.findall(_normalized_text(value)):
        token = token.strip("._+-")
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _clamp_score(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(max(float(value), 0.0), 1.0)


@dataclass(frozen=True)
class RetrievedChunk:
    """One organization-scoped knowledge candidate after retrieval/reranking.

    ``similarity`` intentionally exposes the final retrieval score so existing
    response-grounding code can keep using a single relevance field. Vector
    distance remains available for diagnostics and backward compatibility.
    """

    chunk: Chunk
    distance: float | None
    retrieval_score: float | None = None
    keyword_score: float = 0.0
    retrieval_methods: tuple[str, ...] = ()

    @property
    def semantic_similarity(self) -> float:
        if self.distance is None:
            return 0.0
        return _clamp_score(1.0 - float(self.distance))

    @property
    def similarity(self) -> float:
        if self.retrieval_score is not None:
            return _clamp_score(self.retrieval_score)
        return self.semantic_similarity


class KnowledgeRetrievalService:
    """Organization-scoped retrieval for SHVYA Knowledge Sources.

    Website/URL documents and uploaded files are both stored as ``Document`` /
    ``Chunk`` records, so this service deliberately treats them as one evidence
    layer. Retrieval is hybrid when query text is available:

        semantic candidates (pgvector)
        + exact/keyword candidates
        -> deterministic score fusion / reranking
        -> bounded relevant evidence

    Keyword retrieval is also a fail-safe when an embedding cannot be generated.
    It never crosses organization boundaries and never reads inactive, failed or
    stale document versions.
    """

    DEFAULT_LIMIT = 5
    MAX_LIMIT = 20
    MAX_KEYWORD_CANDIDATES = 100

    def _base_queryset(self, *, organization) -> QuerySet[Chunk]:
        if organization is None:
            raise RetrievalError("Organization is required.")
        return (
            Chunk.objects.filter(
                organization=organization,
                is_active=True,
                document__organization=organization,
                document__is_active=True,
                document__processing_status="completed",
            )
            .select_related("document")
        )

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
        """Retrieve active organization knowledge by cosine distance."""

        self._validate_vector(query_vector)
        limit = self._validate_limit(limit)

        queryset = (
            self._base_queryset(organization=organization)
            .filter(embedding__isnull=False)
            .annotate(distance=CosineDistance("embedding", query_vector))
            .order_by("distance", "document_id", "chunk_index")[:limit]
        )

        results: list[RetrievedChunk] = []
        for chunk in queryset:
            distance = float(chunk.distance)
            score = _clamp_score(1.0 - distance)
            results.append(
                RetrievedChunk(
                    chunk=chunk,
                    distance=distance,
                    retrieval_score=score,
                    keyword_score=0.0,
                    retrieval_methods=("semantic",),
                )
            )
        return results

    # ============================================================
    # KEYWORD / EXACT RETRIEVAL
    # ============================================================

    def retrieve_by_keyword(
        self,
        *,
        organization,
        query_text: str,
        limit: int = DEFAULT_LIMIT,
    ) -> list[RetrievedChunk]:
        """Retrieve exact terminology without requiring an embedding provider.

        This is intentionally deterministic rather than an LLM classifier. It
        favors exact phrases, document/source names, and broad token coverage,
        which is useful for plan names, prices, feature names, policies and other
        terminology that vector similarity can miss.
        """

        limit = self._validate_limit(limit)
        query = _normalized_text(query_text)
        query_tokens = _tokens(query)
        if not query or not query_tokens:
            return []

        candidate_filter = Q()
        for token in query_tokens[:16]:
            candidate_filter |= Q(content__icontains=token)
            candidate_filter |= Q(document__name__icontains=token)
            candidate_filter |= Q(document__source_key__icontains=token)
            candidate_filter |= Q(document__source_url__icontains=token)

        candidate_limit = min(
            self.MAX_KEYWORD_CANDIDATES,
            max(limit * 10, 30),
        )
        candidates = list(
            self._base_queryset(organization=organization)
            .filter(candidate_filter)
            .order_by("document_id", "chunk_index")[:candidate_limit]
        )

        scored: list[RetrievedChunk] = []
        token_count = max(len(query_tokens), 1)
        for chunk in candidates:
            content = _normalized_text(chunk.content)
            name = _normalized_text(chunk.document.name)
            source = _normalized_text(
                f"{chunk.document.source_key or ''} {chunk.document.source_url or ''}"
            )
            searchable = f"{name} {source} {content}"

            matched = sum(1 for token in query_tokens if token in searchable)
            title_matched = sum(
                1 for token in query_tokens if token in name or token in source
            )
            coverage = matched / token_count
            title_coverage = title_matched / token_count

            exact_content = query in content
            exact_title = query in name or query in source
            phrase_bonus = 0.0
            if exact_title:
                phrase_bonus = 0.30
            elif exact_content:
                phrase_bonus = 0.22

            # A single exact product/plan token should remain useful, while a
            # broad multi-token match outranks incidental mentions.
            frequency_bonus = min(
                sum(min(content.count(token), 3) for token in query_tokens)
                / max(token_count * 12, 1),
                0.10,
            )
            score = _clamp_score(
                (coverage * 0.58)
                + (title_coverage * 0.18)
                + phrase_bonus
                + frequency_bonus
            )
            if score <= 0:
                continue
            scored.append(
                RetrievedChunk(
                    chunk=chunk,
                    distance=None,
                    retrieval_score=score,
                    keyword_score=score,
                    retrieval_methods=("keyword",),
                )
            )

        return sorted(
            scored,
            key=lambda item: (
                -item.similarity,
                item.chunk.document_id,
                item.chunk.chunk_index,
            ),
        )[:limit]

    # ============================================================
    # HYBRID RETRIEVAL + RERANKING
    # ============================================================

    def retrieve_hybrid(
        self,
        *,
        organization,
        query_text: str,
        query_vector: list[float] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[RetrievedChunk]:
        """Fuse semantic and keyword evidence into one ranked result set."""

        limit = self._validate_limit(limit)
        candidate_limit = min(self.MAX_LIMIT, max(limit * 3, limit))

        semantic: list[RetrievedChunk] = []
        if query_vector is not None:
            semantic = self.retrieve_by_vector(
                organization=organization,
                query_vector=query_vector,
                limit=candidate_limit,
            )
        keyword = self.retrieve_by_keyword(
            organization=organization,
            query_text=query_text,
            limit=candidate_limit,
        )

        merged: dict[int, dict] = {}
        for item in semantic:
            merged[item.chunk.id] = {
                "chunk": item.chunk,
                "distance": item.distance,
                "semantic": item.semantic_similarity,
                "keyword": 0.0,
                "methods": {"semantic"},
            }
        for item in keyword:
            row = merged.setdefault(
                item.chunk.id,
                {
                    "chunk": item.chunk,
                    "distance": None,
                    "semantic": 0.0,
                    "keyword": 0.0,
                    "methods": set(),
                },
            )
            row["keyword"] = max(float(row["keyword"]), item.keyword_score)
            row["methods"].add("keyword")

        reranked: list[RetrievedChunk] = []
        for row in merged.values():
            semantic_score = _clamp_score(row["semantic"])
            keyword_score = _clamp_score(row["keyword"])
            if semantic_score and keyword_score:
                # Agreement between two independent retrieval signals receives a
                # small bounded bonus, while semantic evidence still dominates.
                final_score = _clamp_score(
                    (semantic_score * 0.68)
                    + (keyword_score * 0.32)
                    + 0.06
                )
            elif semantic_score:
                final_score = semantic_score
            else:
                final_score = _clamp_score(keyword_score * 0.96)

            reranked.append(
                RetrievedChunk(
                    chunk=row["chunk"],
                    distance=row["distance"],
                    retrieval_score=final_score,
                    keyword_score=keyword_score,
                    retrieval_methods=tuple(sorted(row["methods"])),
                )
            )

        return sorted(
            reranked,
            key=lambda item: (
                -item.similarity,
                -item.keyword_score,
                item.chunk.document_id,
                item.chunk.chunk_index,
            ),
        )[:limit]

    # ============================================================
    # VALIDATION
    # ============================================================

    def _validate_vector(self, query_vector: list[float]) -> None:
        if not query_vector:
            raise RetrievalError("Query vector cannot be empty.")

        expected_dimensions = Chunk.EMBEDDING_DIMENSIONS
        actual_dimensions = len(query_vector)
        if actual_dimensions != expected_dimensions:
            raise RetrievalError(
                "Query vector dimension mismatch. "
                f"Expected {expected_dimensions}, received {actual_dimensions}."
            )

        for value in query_vector:
            if not isinstance(value, (int, float)):
                raise RetrievalError(
                    "Query vector must contain only numeric values."
                )

    def _validate_limit(self, limit: int) -> int:
        if limit <= 0:
            raise RetrievalError("Retrieval limit must be greater than zero.")
        if limit > self.MAX_LIMIT:
            raise RetrievalError(
                f"Retrieval limit cannot exceed {self.MAX_LIMIT}."
            )
        return limit
