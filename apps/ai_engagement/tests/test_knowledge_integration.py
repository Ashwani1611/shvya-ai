from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.ai_engagement.models import Chunk, Document, KnowledgeSource
from apps.ai_engagement.services.embedding_index import EmbeddingIndexService
from apps.ai_engagement.services.embeddings import EmbeddingError
from apps.ai_engagement.services.knowledge import (
    KnowledgeExtractionError,
    KnowledgeIngestionService,
)
from apps.ai_engagement.services.knowledge_pipeline import (
    KnowledgePipelineError,
    KnowledgePipelineService,
)
from apps.ai_engagement.services.retrieval import (
    KnowledgeRetrievalService,
)
from apps.organizations.models import Organization


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(
        name="Integration Test Organization",
    )


@pytest.fixture
def other_organization():
    return Organization.objects.create(
        name="Other Integration Organization",
    )


@pytest.fixture
def ingestion_service():
    return KnowledgeIngestionService()


@pytest.fixture
def embedding_service():
    return EmbeddingIndexService()


@pytest.fixture
def retrieval_service():
    return KnowledgeRetrievalService()


@pytest.fixture
def pipeline_service():
    return KnowledgePipelineService()


def make_text_file(
    *,
    name: str = "knowledge.txt",
    content: str = (
        "SHVYA AI helps businesses manage leads, conversations, "
        "follow-ups, and customer engagement."
    ),
):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(
        name,
        content.encode("utf-8"),
        content_type="text/plain",
    )


def make_document(
    *,
    organization,
    source_key: str = "knowledge.txt",
    name: str = "Knowledge",
    version: int = 1,
    is_active: bool = False,
    content: str = (
        "SHVYA AI helps businesses manage leads, conversations, "
        "follow-ups, and customer engagement."
    ),
):
    return Document.objects.create(
        organization=organization,
        name=name,
        source_key=source_key,
        version=version,
        file=make_text_file(
            name=source_key,
            content=content,
        ),
        processing_status=Document.ProcessingStatus.PENDING,
        is_active=is_active,
    )


def make_source(
    *,
    organization,
    url: str = "https://example.com/knowledge",
    name: str = "Example Knowledge",
):
    return KnowledgeSource.objects.create(
        organization=organization,
        source_type=KnowledgeSource.SourceType.URL,
        name=name,
        url=url,
        is_active=True,
    )


def make_vector(
    first_value: float = 1.0,
    second_value: float = 0.0,
) -> list[float]:
    return [
        first_value,
        second_value,
        0.0,
    ] + [0.0] * (Chunk.EMBEDDING_DIMENSIONS - 3)


def long_chunking_content() -> str:
    """
    Generate enough text to exceed the configured chunk size.

    The test intentionally does not assume a particular chunk-size
    constant from the production implementation.
    """
    paragraph = (
        "SHVYA AI manages CRM leads and customer conversations. "
        "Businesses can automate engagement, qualification, "
        "follow-ups, and knowledge-driven responses. "
    )

    return paragraph * 1000


class TestKnowledgeDocumentIntegration:
    """
    Real integration coverage for:

        uploaded file
            ↓
        extraction
            ↓
        chunking
            ↓
        completed document
            ↓
        embedding generation boundary
            ↓
        persisted vectors
            ↓
        pgvector retrieval
    """

    def test_document_ingestion_creates_real_chunks(
        self,
        organization,
        ingestion_service,
    ):
        document = make_document(
            organization=organization,
            content=(
                "SHVYA AI provides CRM automation. "
                "It manages customer conversations. "
                "It also supports automated follow-ups."
            ),
        )

        chunk_count = ingestion_service.ingest_document(
            document
        )

        assert chunk_count > 0

        document.refresh_from_db()

        assert (
            document.processing_status
            == Document.ProcessingStatus.COMPLETED
        )
        assert document.processing_error == ""
        assert document.version == 1
        assert document.is_active is True

        chunks = list(
            document.chunks.order_by("chunk_index")
        )

        assert len(chunks) == chunk_count
        assert chunks
        assert all(
            chunk.is_active
            for chunk in chunks
        )
        assert all(
            chunk.content.strip()
            for chunk in chunks
        )
        assert [
            chunk.chunk_index
            for chunk in chunks
        ] == list(range(len(chunks)))

    def test_document_pipeline_ingests_and_indexes_real_chunks(
        self,
        organization,
        pipeline_service,
    ):
        document = make_document(
            organization=organization,
            content=(
                "SHVYA AI manages leads and customer conversations. "
                "Businesses can use AI to qualify and engage leads."
            ),
        )

        embedding_vector = make_vector()

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=[embedding_vector],
        ):
            processed_document = (
                pipeline_service.process_document(
                    document=document,
                )
            )

        processed_document.refresh_from_db()

        assert (
            processed_document.processing_status
            == Document.ProcessingStatus.COMPLETED
        )
        assert processed_document.is_active is True

        chunks = list(
            processed_document.chunks.order_by(
                "chunk_index"
            )
        )

        assert chunks
        assert all(
            chunk.embedding is not None
            for chunk in chunks
        )

    def test_index_document_only_missing_leaves_existing_embeddings_untouched(
        self,
        organization,
        ingestion_service,
        embedding_service,
    ):
        document = make_document(
            organization=organization,
            content=(
                "First knowledge sentence. "
                "Second knowledge sentence. "
                "Third knowledge sentence."
            ),
        )

        ingestion_service.ingest_document(
            document
        )

        chunks = list(
            document.chunks.order_by("chunk_index")
        )

        assert chunks

        existing_embedding = make_vector()

        chunks[0].embedding = existing_embedding
        chunks[0].save(
            update_fields=["embedding"]
        )

        remaining_count = len(chunks) - 1

        generated_vector = make_vector(
            first_value=0.0,
            second_value=1.0,
        )

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=[
                generated_vector
            ] * remaining_count,
        ) as mock_embed:
            indexed_count = embedding_service.index_document(
                document,
                only_missing=True,
            )

        assert indexed_count == remaining_count

        if remaining_count:
            mock_embed.assert_called_once()

        chunks = list(
            document.chunks.order_by("chunk_index")
        )

        assert chunks[0].embedding == existing_embedding

        for chunk in chunks[1:]:
            assert chunk.embedding is not None

    def test_index_document_can_reindex_all_active_chunks_when_requested(
        self,
        organization,
        ingestion_service,
        embedding_service,
    ):
        document = make_document(
            organization=organization,
            content=(
                "Knowledge A. "
                "Knowledge B."
            ),
        )

        ingestion_service.ingest_document(
            document
        )

        chunks = list(
            document.chunks.order_by("chunk_index")
        )

        initial_vector = make_vector()

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=[
                initial_vector
            ] * len(chunks),
        ):
            first_count = embedding_service.index_document(
                document,
                only_missing=True,
            )

        assert first_count == len(chunks)

        replacement_vector = make_vector(
            first_value=0.0,
            second_value=1.0,
        )

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=[
                replacement_vector
            ] * len(chunks),
        ):
            second_count = embedding_service.index_document(
                document,
                only_missing=False,
            )

        assert second_count == len(chunks)

        refreshed_chunks = list(
            document.chunks.order_by("chunk_index")
        )

        assert all(
            chunk.embedding == replacement_vector
            for chunk in refreshed_chunks
        )


class TestKnowledgeRetrievalIntegration:
    """
    Integration coverage for persisted vectors + real
    PostgreSQL/pgvector similarity retrieval.
    """

    def test_indexed_knowledge_is_retrievable(
        self,
        organization,
        ingestion_service,
        embedding_service,
        retrieval_service,
    ):
        document = make_document(
            organization=organization,
            content=(
                "SHVYA AI handles CRM lead management. "
                "This document contains customer engagement "
                "knowledge."
            ),
        )

        ingestion_service.ingest_document(
            document
        )

        chunks = list(
            document.chunks.order_by("chunk_index")
        )

        assert chunks

        vectors = [
            make_vector()
        ] * len(chunks)

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=vectors,
        ):
            indexed_count = embedding_service.index_document(
                document,
                only_missing=True,
            )

        assert indexed_count == len(chunks)

        results = retrieval_service.retrieve_by_vector(
            organization=organization,
            query_vector=make_vector(),
            limit=5,
        )

        assert results
        assert (
            results[0].chunk.document_id
            == document.id
        )
        assert (
            results[0].chunk.organization_id
            == organization.id
        )
        assert results[0].similarity == pytest.approx(
            1.0,
            abs=1e-6,
        )

    def test_retrieval_respects_organization_isolation(
        self,
        organization,
        other_organization,
        ingestion_service,
        embedding_service,
        retrieval_service,
    ):
        document_a = make_document(
            organization=organization,
            source_key="org-a.txt",
            content=(
                "Organization A private knowledge."
            ),
        )

        document_b = make_document(
            organization=other_organization,
            source_key="org-b.txt",
            content=(
                "Organization B private knowledge."
            ),
        )

        ingestion_service.ingest_document(
            document_a
        )
        ingestion_service.ingest_document(
            document_b
        )

        vector_a = make_vector()

        vector_b = make_vector(
            first_value=0.0,
            second_value=1.0,
        )

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            side_effect=[
                [vector_a],
                [vector_b],
            ],
        ):
            embedding_service.index_document(
                document_a,
                only_missing=True,
            )
            embedding_service.index_document(
                document_b,
                only_missing=True,
            )

        results_a = retrieval_service.retrieve_by_vector(
            organization=organization,
            query_vector=vector_a,
            limit=5,
        )

        results_b = retrieval_service.retrieve_by_vector(
            organization=other_organization,
            query_vector=vector_b,
            limit=5,
        )

        assert results_a
        assert results_b

        assert all(
            result.chunk.organization_id
            == organization.id
            for result in results_a
        )

        assert all(
            result.chunk.organization_id
            == other_organization.id
            for result in results_b
        )

        assert all(
            result.chunk.document_id
            != document_b.id
            for result in results_a
        )

        assert all(
            result.chunk.document_id
            != document_a.id
            for result in results_b
        )

    def test_retrieval_ignores_inactive_document_versions(
        self,
        organization,
        ingestion_service,
        embedding_service,
        retrieval_service,
    ):
        old_document = make_document(
            organization=organization,
            source_key="versioned.txt",
            name="Versioned Knowledge",
            version=1,
            content="Old version knowledge.",
        )

        ingestion_service.ingest_document(
            old_document
        )

        old_vector = make_vector()

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=[old_vector],
        ):
            embedding_service.index_document(
                old_document,
                only_missing=True,
            )

        old_document.refresh_from_db()

        assert old_document.is_active is True

        new_document = make_document(
            organization=organization,
            source_key="versioned.txt",
            name="Versioned Knowledge",
            version=2,
            content="New version knowledge.",
        )

        with patch.object(
            ingestion_service,
            "extract_file_text",
            return_value="New version knowledge.",
        ):
            ingestion_service.ingest_document(
                new_document
            )

        new_document.refresh_from_db()
        old_document.refresh_from_db()

        assert old_document.is_active is False
        assert new_document.is_active is True
        assert new_document.version == 2

        new_chunk = new_document.chunks.first()

        assert new_chunk is not None

        new_vector = make_vector(
            first_value=0.0,
            second_value=1.0,
        )

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=[new_vector],
        ):
            embedding_service.index_document(
                new_document,
                only_missing=True,
            )

        results = retrieval_service.retrieve_by_vector(
            organization=organization,
            query_vector=new_vector,
            limit=5,
        )

        assert results

        assert all(
            result.chunk.document_id
            == new_document.id
            for result in results
        )

        assert all(
            result.chunk.document_id
            != old_document.id
            for result in results
        )

    def test_retrieval_ignores_chunks_without_embeddings(
        self,
        organization,
        ingestion_service,
        retrieval_service,
    ):
        document = make_document(
            organization=organization,
            content=(
                "Knowledge without an embedding."
            ),
        )

        ingestion_service.ingest_document(
            document
        )

        assert document.chunks.filter(
            embedding__isnull=True
        ).exists()

        results = retrieval_service.retrieve_by_vector(
            organization=organization,
            query_vector=make_vector(),
            limit=5,
        )

        assert results == []

    def test_retrieval_applies_requested_limit(
        self,
        organization,
        ingestion_service,
        embedding_service,
        retrieval_service,
    ):
        document = make_document(
            organization=organization,
            content=long_chunking_content(),
        )

        ingestion_service.ingest_document(
            document
        )

        chunks = list(
            document.chunks.order_by("chunk_index")
        )

        assert len(chunks) >= 2

        vector = make_vector()

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=[vector] * len(chunks),
        ):
            embedding_service.index_document(
                document,
                only_missing=True,
            )

        results = retrieval_service.retrieve_by_vector(
            organization=organization,
            query_vector=vector,
            limit=2,
        )

        assert len(results) == 2


class TestKnowledgeUrlVersionIntegration:
    def test_successful_url_processing_publishes_first_version(
        self,
        organization,
        ingestion_service,
    ):
        source = make_source(
            organization=organization,
            url="https://example.com/docs",
        )

        with patch.object(
            ingestion_service,
            "extract_url_text",
            return_value=(
                "SHVYA AI documentation for customers."
            ),
        ):
            chunk_count = ingestion_service.ingest_url(
                source
            )

        assert chunk_count > 0

        document = Document.objects.get(
            organization=organization,
            source_key="https://example.com/docs",
        )

        assert document.version == 1
        assert document.is_active is True
        assert (
            document.processing_status
            == Document.ProcessingStatus.COMPLETED
        )
        assert document.chunks.exists()

    def test_successful_second_url_processing_replaces_active_version(
        self,
        organization,
        ingestion_service,
    ):
        source = make_source(
            organization=organization,
            url="https://example.com/docs",
        )

        with patch.object(
            ingestion_service,
            "extract_url_text",
            return_value="Version one content.",
        ):
            ingestion_service.ingest_url(source)

        first_document = Document.objects.get(
            organization=organization,
            source_key="https://example.com/docs",
            version=1,
        )

        with patch.object(
            ingestion_service,
            "extract_url_text",
            return_value="Version two content.",
        ):
            ingestion_service.ingest_url(source)

        first_document.refresh_from_db()

        second_document = Document.objects.get(
            organization=organization,
            source_key="https://example.com/docs",
            version=2,
        )

        assert first_document.is_active is False
        assert second_document.is_active is True

        assert (
            second_document.processing_status
            == Document.ProcessingStatus.COMPLETED
        )

        assert Document.objects.filter(
            organization=organization,
            source_key="https://example.com/docs",
            is_active=True,
        ).count() == 1

    def test_failed_url_processing_keeps_previous_active_version(
        self,
        organization,
        ingestion_service,
    ):
        source = make_source(
            organization=organization,
            url="https://example.com/docs",
        )

        with patch.object(
            ingestion_service,
            "extract_url_text",
            return_value="Stable version content.",
        ):
            ingestion_service.ingest_url(source)

        first_document = Document.objects.get(
            organization=organization,
            source_key="https://example.com/docs",
            version=1,
        )

        with patch.object(
            ingestion_service,
            "extract_url_text",
            side_effect=KnowledgeExtractionError(
                "Temporary upstream failure."
            ),
        ):
            with pytest.raises(
                KnowledgeExtractionError,
                match="Temporary upstream failure",
            ):
                ingestion_service.ingest_url(source)

        first_document.refresh_from_db()

        assert first_document.is_active is True
        assert (
            first_document.processing_status
            == Document.ProcessingStatus.COMPLETED
        )

        active_documents = Document.objects.filter(
            organization=organization,
            source_key="https://example.com/docs",
            is_active=True,
        )

        assert active_documents.count() == 1
        assert (
            active_documents.first().id
            == first_document.id
        )


class TestKnowledgePipelineIntegration:
    def test_pipeline_process_document_persists_indexed_knowledge(
        self,
        organization,
        pipeline_service,
    ):
        document = make_document(
            organization=organization,
            content=(
                "Pipeline integration content."
            ),
        )

        vector = make_vector()

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            return_value=[vector],
        ):
            result = pipeline_service.process_document(
                document=document,
            )

        result.refresh_from_db()

        assert result.id == document.id
        assert (
            result.processing_status
            == Document.ProcessingStatus.COMPLETED
        )
        assert result.is_active is True

        chunks = list(
            result.chunks.all()
        )

        assert chunks
        assert all(
            chunk.embedding is not None
            for chunk in chunks
        )

    def test_pipeline_wraps_embedding_failure(
        self,
        organization,
        pipeline_service,
    ):
        document = make_document(
            organization=organization,
            content="Pipeline failure content.",
        )

        with patch(
            "apps.ai_engagement.services.embeddings."
            "EmbeddingService.embed_texts",
            side_effect=EmbeddingError(
                "Embedding provider unavailable."
            ),
        ):
            with pytest.raises(
                KnowledgePipelineError,
                match="Knowledge document indexing failed",
            ):
                pipeline_service.process_document(
                    document=document,
                )

        document.refresh_from_db()

        assert (
            document.processing_status
            == Document.ProcessingStatus.COMPLETED
        )
        assert document.is_active is True


class TestKnowledgeDataIntegrity:
    def test_document_versions_are_unique_per_source(
        self,
        organization,
    ):
        make_document(
            organization=organization,
            source_key="unique.txt",
        )

        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            Document.objects.create(
                organization=organization,
                name="Duplicate",
                source_key="unique.txt",
                version=1,
                file=make_text_file(
                    name="duplicate.txt",
                    content="duplicate",
                ),
                processing_status=(
                    Document.ProcessingStatus.PENDING
                ),
                is_active=False,
            )

    def test_different_organizations_can_use_same_source_key(
        self,
        organization,
        other_organization,
    ):
        document_a = make_document(
            organization=organization,
            source_key="shared-name.txt",
        )

        document_b = make_document(
            organization=other_organization,
            source_key="shared-name.txt",
        )

        assert document_a.id != document_b.id
        assert document_a.source_key == document_b.source_key
        assert (
            document_a.organization_id
            != document_b.organization_id
        )