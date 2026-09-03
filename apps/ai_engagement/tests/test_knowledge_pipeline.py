from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import TestCase

from apps.ai_engagement.models import (
    Chunk,
    Document,
    KnowledgeSource,
)
from apps.ai_engagement.services.embedding_index import (
    EmbeddingIndexError,
)
from apps.ai_engagement.services.knowledge import (
    KnowledgeExtractionError,
)
from apps.ai_engagement.services.knowledge_pipeline import (
    KnowledgePipelineError,
    KnowledgePipelineService,
)
from apps.organizations.models import Organization


class KnowledgePipelineServiceTests(TestCase):
    """
    Tests the orchestration between:

        KnowledgeIngestionService
                    +
        EmbeddingIndexService

    External providers are mocked.

    These tests do NOT call:

        - OpenAI
        - external websites
        - Meta
    """

    @classmethod
    def setUpTestData(cls):

        cls.organization = Organization.objects.create(
            name="Knowledge Pipeline Organization",
        )

        cls.other_organization = Organization.objects.create(
            name="Other Knowledge Organization",
        )

    # ========================================================
    # HELPERS
    # ========================================================

    def create_document(
        self,
        *,
        name="guide.txt",
        organization=None,
        source_key="guide.txt",
    ):
        organization = (
            organization
            or self.organization
        )

        return Document.objects.create(
            organization=organization,
            name=name,
            source_key=source_key,
            version=1,
            processing_status=(
                Document.ProcessingStatus.PENDING
            ),
            is_active=True,
        )

    def create_url_source(
        self,
        *,
        organization=None,
        url="https://example.com",
        name="Example",
        is_active=True,
    ):
        organization = (
            organization
            or self.organization
        )

        return KnowledgeSource.objects.create(
            organization=organization,
            source_type=KnowledgeSource.SourceType.URL,
            name=name,
            url=url,
            is_active=is_active,
        )

    # ========================================================
    # DOCUMENT PIPELINE
    # ========================================================

    def test_process_document_ingests_then_indexes(
        self,
    ):
        document = self.create_document()

        ingestion_service = Mock()
        embedding_index_service = Mock()
        ingestion_service.publish_document_version.return_value = document

        refreshed_document = (
            Document.objects.get(
                pk=document.pk,
            )
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=ingestion_service,
            embedding_index_service=embedding_index_service,
        )

        result = pipeline.process_document(
            document=document,
        )

        ingestion_service.ingest_document.assert_called_once_with(
            document,
        )

        embedding_index_service.index_document.assert_called_once_with(
            refreshed_document,
        )

        self.assertEqual(
            result.pk,
            document.pk,
        )

        ingestion_service.publish_document_version.assert_called_once_with(
            refreshed_document,
        )

    def test_process_document_requires_document(
        self,
    ):
        pipeline = KnowledgePipelineService(
            ingestion_service=Mock(),
            embedding_index_service=Mock(),
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ):
            pipeline.process_document(
                document=None,
            )

    def test_document_ingestion_error_is_wrapped(
        self,
    ):
        document = self.create_document()

        ingestion_service = Mock()

        ingestion_service.ingest_document.side_effect = (
            KnowledgeExtractionError(
                "extraction failed",
            )
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=ingestion_service,
            embedding_index_service=Mock(),
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ) as context:

            pipeline.process_document(
                document=document,
            )

        self.assertIn(
            "extraction failed",
            str(
                context.exception,
            ),
        )

    def test_document_embedding_error_is_wrapped(
        self,
    ):
        document = self.create_document()

        embedding_index_service = Mock()

        embedding_index_service.index_document.side_effect = (
            EmbeddingIndexError(
                "embedding failed",
            )
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=Mock(),
            embedding_index_service=embedding_index_service,
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ) as context:

            pipeline.process_document(
                document=document,
            )

        self.assertIn(
            "embedding failed",
            str(
                context.exception,
            ),
        )

    # ========================================================
    # URL PIPELINE
    # ========================================================

    def test_process_url_ingests_then_indexes_latest_active_document(
        self,
    ):
        source = self.create_url_source()

        document = Document.objects.create(
            organization=self.organization,
            name="Example",
            source_key="https://example.com",
            version=1,
            source_url="https://example.com",
            processing_status=(
                Document.ProcessingStatus.COMPLETED
            ),
            is_active=True,
        )

        ingestion_service = Mock()

        ingestion_service._normalize_url.return_value = (
            "https://example.com"
        )
        ingestion_service.publish_document_version.return_value = document

        embedding_index_service = Mock()

        pipeline = KnowledgePipelineService(
            ingestion_service=ingestion_service,
            embedding_index_service=embedding_index_service,
        )

        result = pipeline.process_url(
            source=source,
        )

        ingestion_service.ingest_url.assert_called_once_with(
            source,
        )

        embedding_index_service.index_document.assert_called_once_with(
            document,
        )

        self.assertEqual(
            result.pk,
            document.pk,
        )

        ingestion_service.publish_document_version.assert_called_once_with(
            document,
        )

    def test_process_url_uses_latest_version(
        self,
    ):
        source = self.create_url_source()

        old_document = Document.objects.create(
            organization=self.organization,
            name="Example",
            source_key="https://example.com",
            version=1,
            source_url="https://example.com",
            processing_status=(
                Document.ProcessingStatus.COMPLETED
            ),
            is_active=False,
        )

        latest_document = Document.objects.create(
            organization=self.organization,
            name="Example",
            source_key="https://example.com",
            version=2,
            source_url="https://example.com",
            processing_status=(
                Document.ProcessingStatus.COMPLETED
            ),
            is_active=True,
        )

        ingestion_service = Mock()

        ingestion_service._normalize_url.return_value = (
            "https://example.com"
        )
        ingestion_service.publish_document_version.return_value = (
            latest_document
        )

        embedding_index_service = Mock()

        pipeline = KnowledgePipelineService(
            ingestion_service=ingestion_service,
            embedding_index_service=embedding_index_service,
        )

        result = pipeline.process_url(
            source=source,
        )

        self.assertEqual(
            result.pk,
            latest_document.pk,
        )

        self.assertNotEqual(
            result.pk,
            old_document.pk,
        )

        embedding_index_service.index_document.assert_called_once_with(
            latest_document,
        )

        ingestion_service.publish_document_version.assert_called_once_with(
            latest_document,
        )

    def test_process_url_requires_url_source(
        self,
    ):
        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.FILE,
            name="File Source",
            is_active=True,
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=Mock(),
            embedding_index_service=Mock(),
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ):
            pipeline.process_url(
                source=source,
            )

    def test_process_url_rejects_inactive_source(
        self,
    ):
        source = self.create_url_source(
            is_active=False,
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=Mock(),
            embedding_index_service=Mock(),
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ):
            pipeline.process_url(
                source=source,
            )

    def test_process_url_requires_resulting_document(
        self,
    ):
        source = self.create_url_source()

        ingestion_service = Mock()

        ingestion_service._normalize_url.return_value = (
            "https://example.com"
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=ingestion_service,
            embedding_index_service=Mock(),
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ) as context:

            pipeline.process_url(
                source=source,
            )

        self.assertIn(
            "no completed Document version was found",
            str(
                context.exception,
            ),
        )

    def test_url_ingestion_error_is_wrapped(
        self,
    ):
        source = self.create_url_source()

        ingestion_service = Mock()

        ingestion_service.ingest_url.side_effect = (
            KnowledgeExtractionError(
                "url fetch failed",
            )
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=ingestion_service,
            embedding_index_service=Mock(),
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ) as context:

            pipeline.process_url(
                source=source,
            )

        self.assertIn(
            "url fetch failed",
            str(
                context.exception,
            ),
        )

    def test_url_embedding_error_is_wrapped(
        self,
    ):
        source = self.create_url_source()

        document = Document.objects.create(
            organization=self.organization,
            name="Example",
            source_key="https://example.com",
            version=1,
            source_url="https://example.com",
            processing_status=(
                Document.ProcessingStatus.COMPLETED
            ),
            is_active=True,
        )

        ingestion_service = Mock()

        ingestion_service._normalize_url.return_value = (
            "https://example.com"
        )

        embedding_index_service = Mock()

        embedding_index_service.index_document.side_effect = (
            EmbeddingIndexError(
                "URL embedding failed",
            )
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=ingestion_service,
            embedding_index_service=embedding_index_service,
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ) as context:

            pipeline.process_url(
                source=source,
            )

        self.assertIn(
            "URL embedding failed",
            str(
                context.exception,
            ),
        )

    # ========================================================
    # REINDEX
    # ========================================================

    def test_reindex_document_indexes_only_missing_embeddings(
        self,
    ):
        document = self.create_document()

        embedding_index_service = Mock()

        embedding_index_service.index_document.return_value = 4

        pipeline = KnowledgePipelineService(
            ingestion_service=Mock(),
            embedding_index_service=embedding_index_service,
        )

        result = pipeline.reindex_document(
            document=document,
        )

        self.assertEqual(
            result,
            4,
        )

        embedding_index_service.index_document.assert_called_once_with(
            document,
            only_missing=True,
        )

    def test_reindex_requires_document(
        self,
    ):
        pipeline = KnowledgePipelineService(
            ingestion_service=Mock(),
            embedding_index_service=Mock(),
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ):
            pipeline.reindex_document(
                document=None,
            )

    def test_reindex_embedding_error_is_wrapped(
        self,
    ):
        document = self.create_document()

        embedding_index_service = Mock()

        embedding_index_service.index_document.side_effect = (
            EmbeddingIndexError(
                "reindex failed",
            )
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=Mock(),
            embedding_index_service=embedding_index_service,
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ) as context:

            pipeline.reindex_document(
                document=document,
            )

        self.assertIn(
            "reindex failed",
            str(
                context.exception,
            ),
        )

    # ========================================================
    # ORGANIZATION ISOLATION
    # ========================================================

    def test_url_pipeline_only_uses_same_organization(
        self,
    ):
        source = self.create_url_source(
            organization=self.organization,
            url="https://example.com",
        )

        Document.objects.create(
            organization=self.other_organization,
            name="Other Organization",
            source_key="https://example.com",
            version=1,
            source_url="https://example.com",
            processing_status=(
                Document.ProcessingStatus.COMPLETED
            ),
            is_active=True,
        )

        ingestion_service = Mock()

        ingestion_service._normalize_url.return_value = (
            "https://example.com"
        )

        pipeline = KnowledgePipelineService(
            ingestion_service=ingestion_service,
            embedding_index_service=Mock(),
        )

        with self.assertRaises(
            KnowledgePipelineError,
        ):
            pipeline.process_url(
                source=source,
            )