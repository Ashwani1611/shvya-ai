from __future__ import annotations

from unittest.mock import patch

from celery.exceptions import Retry
from django.test import TestCase

from apps.ai_engagement.models import Document, KnowledgeSource
from apps.ai_engagement.services.embedding_index import (
    EmbeddingIndexError,
    EmbeddingIndexService,
)
from apps.ai_engagement.services.knowledge import KnowledgeIngestionService
from apps.ai_engagement.tasks import (
    ingest_and_index_document,
    ingest_and_index_url_source,
    reindex_document_embeddings,
)
from apps.organizations.models import Organization


class KnowledgeTaskHardeningTests(TestCase):
    """
    Regression coverage for RAG production hardening.

    These tests execute the real Celery task bodies with `.run()`.
    External embedding/network work is mocked.
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Knowledge Task Organization",
        )
        cls.other_organization = Organization.objects.create(
            name="Other Knowledge Task Organization",
        )

    def create_document(
        self,
        *,
        organization=None,
        source_key="guide.txt",
        version=1,
        is_active=False,
        status=None,
    ):
        organization = organization or self.organization
        return Document.objects.create(
            organization=organization,
            name=f"Guide v{version}",
            source_key=source_key,
            version=version,
            processing_status=(
                status
                if status is not None
                else Document.ProcessingStatus.PENDING
            ),
            is_active=is_active,
        )

    def create_url_source(
        self,
        *,
        organization=None,
        url="https://example.com/knowledge",
    ):
        organization = organization or self.organization
        return KnowledgeSource.objects.create(
            organization=organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Example Knowledge",
            url=url,
            is_active=True,
        )

    def mark_ingested_document_completed(self, document, chunk_count=1):
        document.processing_status = Document.ProcessingStatus.COMPLETED
        document.processing_error = ""
        document.is_active = False
        document.save(
            update_fields=[
                "processing_status",
                "processing_error",
                "is_active",
                "updated_at",
            ]
        )
        return chunk_count

    @patch.object(EmbeddingIndexService, "index_document")
    @patch.object(KnowledgeIngestionService, "ingest_document")
    def test_file_version_is_published_only_after_embedding_succeeds(
        self,
        mocked_ingestion_service,
        mocked_embedding_service,
    ):
        old_document = self.create_document(
            source_key="versioned.txt",
            version=1,
            is_active=True,
            status=Document.ProcessingStatus.COMPLETED,
        )
        new_document = self.create_document(
            source_key="versioned.txt",
            version=2,
            is_active=False,
        )
        mocked_ingestion_service.side_effect = self.mark_ingested_document_completed
        observed_active_state = []

        def index_document(document):
            observed_active_state.append(document.is_active)
            return 1

        mocked_embedding_service.side_effect = index_document
        result = ingest_and_index_document.run(
            document_id=new_document.id,
            organization_id=self.organization.id,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(observed_active_state, [False])
        old_document.refresh_from_db()
        new_document.refresh_from_db()
        self.assertFalse(old_document.is_active)
        self.assertTrue(new_document.is_active)
        self.assertEqual(
            new_document.processing_status,
            Document.ProcessingStatus.COMPLETED,
        )

    @patch.object(EmbeddingIndexService, "index_document")
    @patch.object(KnowledgeIngestionService, "ingest_document")
    def test_file_embedding_failure_marks_new_version_failed_and_keeps_old_active(
        self,
        mocked_ingestion_service,
        mocked_embedding_service,
    ):
        old_document = self.create_document(
            source_key="versioned-failure.txt",
            version=1,
            is_active=True,
            status=Document.ProcessingStatus.COMPLETED,
        )
        new_document = self.create_document(
            source_key="versioned-failure.txt",
            version=2,
            is_active=False,
        )
        mocked_ingestion_service.side_effect = self.mark_ingested_document_completed
        mocked_embedding_service.side_effect = EmbeddingIndexError(
            "embedding unavailable"
        )
        result = ingest_and_index_document.run(
            document_id=new_document.id,
            organization_id=self.organization.id,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "embedding_failed")
        old_document.refresh_from_db()
        new_document.refresh_from_db()
        self.assertTrue(old_document.is_active)
        self.assertEqual(
            old_document.processing_status,
            Document.ProcessingStatus.COMPLETED,
        )
        self.assertFalse(new_document.is_active)
        self.assertEqual(
            new_document.processing_status,
            Document.ProcessingStatus.FAILED,
        )
        self.assertEqual(new_document.processing_error, "embedding unavailable")

    @patch.object(EmbeddingIndexService, "index_document")
    @patch.object(KnowledgeIngestionService, "ingest_document")
    def test_file_task_cannot_access_document_from_another_organization(
        self,
        mocked_ingestion_service,
        mocked_embedding_service,
    ):
        document = self.create_document(
            organization=self.other_organization,
            source_key="other-org.txt",
        )

        # The uploaded-document task intentionally retries a missing
        # organization-scoped lookup to cover transaction visibility.
        # A document from another organization follows that same safe
        # path and must never reach ingestion or embedding.
        with self.assertRaises(Retry):
            ingest_and_index_document.run(
                document_id=document.id,
                organization_id=self.organization.id,
            )

        mocked_ingestion_service.assert_not_called()
        mocked_embedding_service.assert_not_called()

    @patch.object(EmbeddingIndexService, "index_document")
    @patch.object(KnowledgeIngestionService, "ingest_url")
    @patch.object(KnowledgeIngestionService, "normalize_url")
    def test_url_version_is_published_only_after_embedding_succeeds(
        self,
        mocked_normalize_url,
        mocked_ingestion_service,
        mocked_embedding_service,
    ):
        source = self.create_url_source(url="https://example.com/versioned")
        old_document = self.create_document(
            source_key=source.url,
            version=1,
            is_active=True,
            status=Document.ProcessingStatus.COMPLETED,
        )

        def ingest_url(source_obj):
            self.create_document(
                source_key=source_obj.url,
                version=2,
                is_active=False,
                status=Document.ProcessingStatus.COMPLETED,
            )
            return 1

        mocked_ingestion_service.side_effect = ingest_url
        mocked_normalize_url.return_value = source.url
        observed_active_state = []

        def index_document(document):
            observed_active_state.append(document.is_active)
            return 1

        mocked_embedding_service.side_effect = index_document
        result = ingest_and_index_url_source.run(
            source_id=source.id,
            organization_id=self.organization.id,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(observed_active_state, [False])
        old_document.refresh_from_db()
        new_document = Document.objects.get(
            organization=self.organization,
            source_key=source.url,
            version=2,
        )
        self.assertFalse(old_document.is_active)
        self.assertTrue(new_document.is_active)
        self.assertEqual(
            new_document.processing_status,
            Document.ProcessingStatus.COMPLETED,
        )

    @patch.object(EmbeddingIndexService, "index_document")
    @patch.object(KnowledgeIngestionService, "ingest_url")
    @patch.object(KnowledgeIngestionService, "normalize_url")
    def test_url_embedding_failure_marks_new_version_failed_and_keeps_old_active(
        self,
        mocked_normalize_url,
        mocked_ingestion_service,
        mocked_embedding_service,
    ):
        source = self.create_url_source(
            url="https://example.com/versioned-failure"
        )
        old_document = self.create_document(
            source_key=source.url,
            version=1,
            is_active=True,
            status=Document.ProcessingStatus.COMPLETED,
        )

        def ingest_url(source_obj):
            self.create_document(
                source_key=source_obj.url,
                version=2,
                is_active=False,
                status=Document.ProcessingStatus.COMPLETED,
            )
            return 1

        mocked_ingestion_service.side_effect = ingest_url
        mocked_normalize_url.return_value = source.url
        mocked_embedding_service.side_effect = EmbeddingIndexError(
            "url embedding unavailable"
        )
        result = ingest_and_index_url_source.run(
            source_id=source.id,
            organization_id=self.organization.id,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "embedding_failed")
        old_document.refresh_from_db()
        new_document = Document.objects.get(
            organization=self.organization,
            source_key=source.url,
            version=2,
        )
        self.assertTrue(old_document.is_active)
        self.assertFalse(new_document.is_active)
        self.assertEqual(
            new_document.processing_status,
            Document.ProcessingStatus.FAILED,
        )
        self.assertEqual(
            new_document.processing_error,
            "url embedding unavailable",
        )

    @patch.object(EmbeddingIndexService, "index_document")
    @patch.object(KnowledgeIngestionService, "ingest_url")
    @patch.object(KnowledgeIngestionService, "normalize_url")
    def test_url_task_cannot_access_source_from_another_organization(
        self,
        mocked_normalize_url,
        mocked_ingestion_service,
        mocked_embedding_service,
    ):
        source = self.create_url_source(
            organization=self.other_organization,
            url="https://example.com/other-org",
        )
        result = ingest_and_index_url_source.run(
            source_id=source.id,
            organization_id=self.organization.id,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "source_not_found")
        mocked_normalize_url.assert_not_called()
        mocked_ingestion_service.assert_not_called()
        mocked_embedding_service.assert_not_called()

    @patch.object(EmbeddingIndexService, "index_document")
    def test_reindex_can_access_document_only_in_same_organization(
        self,
        mocked_embedding_service,
    ):
        document = self.create_document(
            organization=self.organization,
            source_key="reindex.txt",
            version=1,
            is_active=False,
            status=Document.ProcessingStatus.COMPLETED,
        )
        mocked_embedding_service.return_value = 3
        result = reindex_document_embeddings.run(
            document_id=document.id,
            organization_id=self.organization.id,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["indexed_count"], 3)
        mocked_embedding_service.assert_called_once_with(
            document,
            only_missing=False,
        )
        document.refresh_from_db()
        self.assertFalse(document.is_active)

    @patch.object(EmbeddingIndexService, "index_document")
    def test_reindex_cannot_access_document_from_another_organization(
        self,
        mocked_embedding_service,
    ):
        document = self.create_document(
            organization=self.other_organization,
            source_key="other-org-reindex.txt",
            version=1,
            is_active=False,
            status=Document.ProcessingStatus.COMPLETED,
        )
        result = reindex_document_embeddings.run(
            document_id=document.id,
            organization_id=self.organization.id,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "document_not_found")
        mocked_embedding_service.assert_not_called()
