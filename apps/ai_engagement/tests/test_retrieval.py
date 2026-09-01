from __future__ import annotations

from django.test import TestCase

from apps.ai_engagement.models import (
    Chunk,
    Document,
)
from apps.ai_engagement.services.retrieval import (
    KnowledgeRetrievalService,
    RetrievalError,
)
from apps.organizations.models import Organization


class KnowledgeRetrievalServiceTests(TestCase):
    """
    Tests production knowledge retrieval behavior.

    These tests do NOT call:

        - OpenAI
        - embedding provider
        - external websites
        - Meta

    pgvector is exercised using deterministic vectors stored
    directly in the test database.
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Retrieval Test Organization",
        )

        cls.other_organization = Organization.objects.create(
            name="Other Retrieval Organization",
        )

        cls.query_vector = [1.0] + [0.0] * (
            Chunk.EMBEDDING_DIMENSIONS - 1
        )

    # ========================================================
    # HELPERS
    # ========================================================

    def create_document(
        self,
        *,
        organization=None,
        name="Knowledge Document",
        source_key=None,
        version=1,
        is_active=True,
        processing_status=(
            Document.ProcessingStatus.COMPLETED
        ),
    ):
        organization = (
            organization
            or self.organization
        )

        source_key = (
            source_key
            or f"source-{name}"
        )

        return Document.objects.create(
            organization=organization,
            name=name,
            source_key=source_key,
            version=version,
            processing_status=processing_status,
            is_active=is_active,
        )

    def create_chunk(
        self,
        *,
        document,
        content,
        chunk_index=0,
        embedding=None,
        is_active=True,
    ):
        return Chunk.objects.create(
            document=document,
            organization=document.organization,
            content=content,
            chunk_index=chunk_index,
            embedding=embedding,
            is_active=is_active,
        )

    # ========================================================
    # BASIC RETRIEVAL
    # ========================================================

    def test_retrieves_matching_active_completed_chunk(
        self,
    ):
        document = self.create_document(
            name="Relevant Document",
        )

        chunk = self.create_chunk(
            document=document,
            content="Highly relevant information.",
            embedding=self.query_vector,
        )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
        )

        self.assertEqual(
            len(results),
            1,
        )

        self.assertEqual(
            results[0].chunk.id,
            chunk.id,
        )

        self.assertAlmostEqual(
            results[0].similarity,
            1.0,
            places=5,
        )

    # ========================================================
    # ORGANIZATION ISOLATION
    # ========================================================

    def test_other_organization_knowledge_is_never_returned(
        self,
    ):
        own_document = self.create_document(
            name="Own Document",
            source_key="own-source",
        )

        other_document = self.create_document(
            organization=self.other_organization,
            name="Other Organization Document",
            source_key="other-source",
        )

        own_chunk = self.create_chunk(
            document=own_document,
            content="Organization owned information.",
            embedding=self.query_vector,
        )

        self.create_chunk(
            document=other_document,
            content="Should never be returned.",
            embedding=self.query_vector,
        )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
        )

        self.assertEqual(
            len(results),
            1,
        )

        self.assertEqual(
            results[0].chunk.id,
            own_chunk.id,
        )

        self.assertEqual(
            results[0].chunk.organization_id,
            self.organization.id,
        )

    # ========================================================
    # DOCUMENT STATUS FILTERS
    # ========================================================

    def test_inactive_document_is_excluded(
        self,
    ):
        document = self.create_document(
            name="Inactive Document",
            is_active=False,
        )

        self.create_chunk(
            document=document,
            content="Inactive document content.",
            embedding=self.query_vector,
        )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
        )

        self.assertEqual(
            results,
            [],
        )

    def test_processing_document_is_excluded(
        self,
    ):
        document = self.create_document(
            name="Processing Document",
            processing_status=(
                Document.ProcessingStatus.PROCESSING
            ),
        )

        self.create_chunk(
            document=document,
            content="Still processing.",
            embedding=self.query_vector,
        )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
        )

        self.assertEqual(
            results,
            [],
        )

    def test_pending_document_is_excluded(
        self,
    ):
        document = self.create_document(
            name="Pending Document",
            processing_status=(
                Document.ProcessingStatus.PENDING
            ),
        )

        self.create_chunk(
            document=document,
            content="Pending content.",
            embedding=self.query_vector,
        )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
        )

        self.assertEqual(
            results,
            [],
        )

    def test_failed_document_is_excluded(
        self,
    ):
        document = self.create_document(
            name="Failed Document",
            processing_status=(
                Document.ProcessingStatus.FAILED
            ),
        )

        self.create_chunk(
            document=document,
            content="Failed content.",
            embedding=self.query_vector,
        )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
        )

        self.assertEqual(
            results,
            [],
        )

    # ========================================================
    # CHUNK STATUS FILTERS
    # ========================================================

    def test_inactive_chunk_is_excluded(
        self,
    ):
        document = self.create_document(
            name="Chunk Status Document",
        )

        self.create_chunk(
            document=document,
            content="Inactive chunk.",
            embedding=self.query_vector,
            is_active=False,
        )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
        )

        self.assertEqual(
            results,
            [],
        )

    def test_chunk_without_embedding_is_excluded(
        self,
    ):
        document = self.create_document(
            name="Missing Embedding Document",
        )

        self.create_chunk(
            document=document,
            content="No vector available.",
            embedding=None,
        )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
        )

        self.assertEqual(
            results,
            [],
        )

    # ========================================================
    # SIMILARITY ORDERING
    # ========================================================

    def test_results_are_ordered_by_similarity(
        self,
    ):
        document = self.create_document(
            name="Similarity Document",
        )

        exact_chunk = self.create_chunk(
            document=document,
            content="Exact match.",
            chunk_index=0,
            embedding=self.query_vector,
        )

        # This vector is not collinear with the query vector.
        #
        # Query:
        #     [1, 0, 0, ...]
        #
        # Partial:
        #     [0.5, 0.866..., 0, ...]
        #
        # Therefore cosine similarity is approximately 0.5,
        # rather than 1.0.
        partial_vector = [
            0.5,
            0.8660254037844386,
        ] + [
            0.0
        ] * (
            Chunk.EMBEDDING_DIMENSIONS - 2
        )

        partial_chunk = self.create_chunk(
            document=document,
            content="Partial match.",
            chunk_index=1,
            embedding=partial_vector,
        )

        results = (
            KnowledgeRetrievalService().retrieve_by_vector(
                organization=self.organization,
                query_vector=self.query_vector,
                limit=2,
            )
        )

        self.assertEqual(
            len(results),
            2,
        )

        self.assertEqual(
            results[0].chunk.id,
            exact_chunk.id,
        )

        self.assertEqual(
            results[1].chunk.id,
            partial_chunk.id,
        )

        self.assertAlmostEqual(
            results[0].similarity,
            1.0,
            places=5,
        )

        self.assertAlmostEqual(
            results[1].similarity,
            0.5,
            places=5,
        )

        self.assertGreater(
            results[0].similarity,
            results[1].similarity,
        )

    # ========================================================
    # LIMIT
    # ========================================================

    def test_limit_is_respected(
        self,
    ):
        document = self.create_document(
            name="Limit Document",
        )

        for index in range(5):
            self.create_chunk(
                document=document,
                content=f"Chunk {index}",
                chunk_index=index,
                embedding=self.query_vector,
            )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
            limit=2,
        )

        self.assertEqual(
            len(results),
            2,
        )

    def test_maximum_limit_is_accepted(
        self,
    ):
        document = self.create_document(
            name="Maximum Limit Document",
        )

        for index in range(
            KnowledgeRetrievalService.MAX_LIMIT
        ):
            self.create_chunk(
                document=document,
                content=f"Chunk {index}",
                chunk_index=index,
                embedding=self.query_vector,
            )

        service = KnowledgeRetrievalService()

        results = service.retrieve_by_vector(
            organization=self.organization,
            query_vector=self.query_vector,
            limit=KnowledgeRetrievalService.MAX_LIMIT,
        )

        self.assertEqual(
            len(results),
            KnowledgeRetrievalService.MAX_LIMIT,
        )

    # ========================================================
    # INVALID VECTOR
    # ========================================================

    def test_empty_query_vector_is_rejected(
        self,
    ):
        service = KnowledgeRetrievalService()

        with self.assertRaises(
            RetrievalError,
        ):
            service.retrieve_by_vector(
                organization=self.organization,
                query_vector=[],
            )

    def test_wrong_dimension_query_vector_is_rejected(
        self,
    ):
        service = KnowledgeRetrievalService()

        with self.assertRaises(
            RetrievalError,
        ):
            service.retrieve_by_vector(
                organization=self.organization,
                query_vector=[1.0, 0.0],
            )

    def test_non_numeric_query_vector_is_rejected(
        self,
    ):
        service = KnowledgeRetrievalService()

        invalid_vector = [
            "not-a-number"
        ] + [0.0] * (
            Chunk.EMBEDDING_DIMENSIONS - 1
        )

        with self.assertRaises(
            RetrievalError,
        ):
            service.retrieve_by_vector(
                organization=self.organization,
                query_vector=invalid_vector,
            )

    # ========================================================
    # INVALID LIMIT
    # ========================================================

    def test_zero_limit_is_rejected(
        self,
    ):
        service = KnowledgeRetrievalService()

        with self.assertRaises(
            RetrievalError,
        ):
            service.retrieve_by_vector(
                organization=self.organization,
                query_vector=self.query_vector,
                limit=0,
            )

    def test_negative_limit_is_rejected(
        self,
    ):
        service = KnowledgeRetrievalService()

        with self.assertRaises(
            RetrievalError,
        ):
            service.retrieve_by_vector(
                organization=self.organization,
                query_vector=self.query_vector,
                limit=-1,
            )

    def test_limit_above_maximum_is_rejected(
        self,
    ):
        service = KnowledgeRetrievalService()

        with self.assertRaises(
            RetrievalError,
        ):
            service.retrieve_by_vector(
                organization=self.organization,
                query_vector=self.query_vector,
                limit=(
                    KnowledgeRetrievalService.MAX_LIMIT
                    + 1
                ),
            )