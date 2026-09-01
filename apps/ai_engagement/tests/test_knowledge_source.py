from __future__ import annotations

from unittest.mock import patch

from django.core.files.uploadedfile import (
    SimpleUploadedFile,
)
from django.test import TestCase

from apps.ai_engagement.models import (
    Document,
    KnowledgeSource,
)
from apps.ai_engagement.services.knowledge import (
    KnowledgeExtractionError,
)
from apps.ai_engagement.services.knowledge_source import (
    KnowledgeSourceService,
    KnowledgeSourceServiceError,
)
from apps.organizations.models import Organization


class KnowledgeSourceServiceTests(TestCase):
    """
    Tests the organization-scoped KnowledgeSource management
    service.

    These tests mock the ingestion service where appropriate.

    They do NOT call:

        - external websites
        - OpenAI
        - embeddings
        - pgvector retrieval
        - Meta
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Knowledge Source Test Organization",
        )

        cls.other_organization = Organization.objects.create(
            name="Other Knowledge Organization",
        )

    # ========================================================
    # URL SOURCE
    # ========================================================

    def test_create_url_source(
        self,
    ):
        service = KnowledgeSourceService()

        source = service.create_url_source(
            organization=self.organization,
            url="example.com/pricing",
            name="Pricing Page",
        )

        self.assertEqual(
            source.organization_id,
            self.organization.id,
        )

        self.assertEqual(
            source.source_type,
            KnowledgeSource.SourceType.URL,
        )

        self.assertEqual(
            source.url,
            "https://example.com/pricing",
        )

        self.assertEqual(
            source.name,
            "Pricing Page",
        )

        self.assertTrue(
            source.is_active,
        )

    def test_create_url_source_uses_url_as_default_name(
        self,
    ):
        service = KnowledgeSourceService()

        source = service.create_url_source(
            organization=self.organization,
            url="https://example.com",
        )

        self.assertEqual(
            source.name,
            "https://example.com",
        )

    def test_invalid_url_is_rejected(
        self,
    ):
        service = KnowledgeSourceService()

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.create_url_source(
                organization=self.organization,
                url="ftp://example.com",
            )

    def test_missing_organization_is_rejected_for_url(
        self,
    ):
        service = KnowledgeSourceService()

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.create_url_source(
                organization=None,
                url="https://example.com",
            )

    # ========================================================
    # FILE SOURCE
    # ========================================================

    def test_create_file_source(
        self,
    ):
        service = KnowledgeSourceService()

        uploaded_file = SimpleUploadedFile(
            "pricing.txt",
            b"Pricing information",
            content_type="text/plain",
        )

        source, document = (
            service.create_file_source(
                organization=self.organization,
                uploaded_file=uploaded_file,
            )
        )

        self.assertEqual(
            source.organization_id,
            self.organization.id,
        )

        self.assertEqual(
            source.source_type,
            KnowledgeSource.SourceType.FILE,
        )

        self.assertEqual(
            source.name,
            "pricing.txt",
        )

        self.assertTrue(
            source.is_active,
        )

        self.assertEqual(
            document.organization_id,
            self.organization.id,
        )

        self.assertEqual(
            document.name,
            "pricing.txt",
        )

        self.assertEqual(
            document.source_key,
            "pricing.txt",
        )

        self.assertEqual(
            document.version,
            1,
        )

        self.assertEqual(
            document.processing_status,
            Document.ProcessingStatus.PENDING,
        )

        self.assertTrue(
            bool(document.file),
        )

    def test_create_file_source_uses_custom_name(
        self,
    ):
        service = KnowledgeSourceService()

        uploaded_file = SimpleUploadedFile(
            "pricing.txt",
            b"Pricing information",
            content_type="text/plain",
        )

        source, document = (
            service.create_file_source(
                organization=self.organization,
                uploaded_file=uploaded_file,
                name="Pricing Knowledge",
            )
        )

        self.assertEqual(
            source.name,
            "Pricing Knowledge",
        )

        self.assertEqual(
            document.name,
            "Pricing Knowledge",
        )

    def test_create_file_source_rejects_unsupported_extension(
        self,
    ):
        service = KnowledgeSourceService()

        uploaded_file = SimpleUploadedFile(
            "malware.exe",
            b"not supported",
            content_type="application/octet-stream",
        )

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.create_file_source(
                organization=self.organization,
                uploaded_file=uploaded_file,
            )

    def test_create_file_source_requires_file(
        self,
    ):
        service = KnowledgeSourceService()

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.create_file_source(
                organization=self.organization,
                uploaded_file=None,
            )

    def test_create_file_source_requires_organization(
        self,
    ):
        service = KnowledgeSourceService()

        uploaded_file = SimpleUploadedFile(
            "guide.txt",
            b"Guide content",
            content_type="text/plain",
        )

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.create_file_source(
                organization=None,
                uploaded_file=uploaded_file,
            )

    # ========================================================
    # URL PROCESSING
    # ========================================================

    def test_process_url_source_delegates_to_ingestion(
        self,
    ):
        service = KnowledgeSourceService()

        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Example",
            url="https://example.com",
            is_active=True,
        )

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

        with patch.object(
            service.ingestion_service,
            "_normalize_url",
            return_value="https://example.com",
        ), patch.object(
            service.ingestion_service,
            "ingest_url",
            return_value=1,
        ) as mocked_ingest:

            result = (
                service.process_url_source(
                    source=source,
                )
            )

        mocked_ingest.assert_called_once_with(
            source,
        )

        self.assertEqual(
            result.id,
            document.id,
        )

    def test_process_url_source_requires_url_source(
        self,
    ):
        service = KnowledgeSourceService()

        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.FILE,
            name="File Source",
            is_active=True,
        )

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.process_url_source(
                source=source,
            )

    def test_process_url_source_rejects_inactive_source(
        self,
    ):
        service = KnowledgeSourceService()

        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Inactive Source",
            url="https://example.com",
            is_active=False,
        )

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.process_url_source(
                source=source,
            )

    # ========================================================
    # FILE PROCESSING
    # ========================================================

    def test_process_file_document_delegates_to_ingestion(
        self,
    ):
        service = KnowledgeSourceService()

        uploaded_file = SimpleUploadedFile(
            "guide.txt",
            b"Guide content",
            content_type="text/plain",
        )

        document = Document.objects.create(
            organization=self.organization,
            name="guide.txt",
            source_key="guide.txt",
            version=1,
            file=uploaded_file,
            processing_status=(
                Document.ProcessingStatus.PENDING
            ),
            is_active=True,
        )

        with patch.object(
            service.ingestion_service,
            "ingest_document",
            return_value=3,
        ) as mocked_ingest:

            result = (
                service.process_file_source(
                    document=document,
                )
            )

        mocked_ingest.assert_called_once_with(
            document,
        )

        self.assertEqual(
            result,
            3,
        )

    def test_process_file_document_requires_document(
        self,
    ):
        service = KnowledgeSourceService()

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.process_file_source(
                document=None,
            )

    # ========================================================
    # ERROR TRANSLATION
    # ========================================================

    def test_url_processing_error_is_wrapped(
        self,
    ):
        service = KnowledgeSourceService()

        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Broken Source",
            url="https://example.com",
            is_active=True,
        )

        with patch.object(
            service.ingestion_service,
            "ingest_url",
            side_effect=KnowledgeExtractionError(
                "fetch failed",
            ),
        ):

            with self.assertRaises(
                KnowledgeSourceServiceError,
            ) as context:

                service.process_url_source(
                    source=source,
                )

        self.assertIn(
            "fetch failed",
            str(
                context.exception,
            ),
        )

    def test_file_processing_error_is_wrapped(
        self,
    ):
        service = KnowledgeSourceService()

        document = Document.objects.create(
            organization=self.organization,
            name="broken.txt",
            source_key="broken.txt",
            version=1,
            processing_status=(
                Document.ProcessingStatus.PENDING
            ),
            is_active=True,
        )

        with patch.object(
            service.ingestion_service,
            "ingest_document",
            side_effect=KnowledgeExtractionError(
                "extraction failed",
            ),
        ):

            with self.assertRaises(
                KnowledgeSourceServiceError,
            ) as context:

                service.process_file_source(
                    document=document,
                )

        self.assertIn(
            "extraction failed",
            str(
                context.exception,
            ),
        )

    # ========================================================
    # ACTIVATE / DEACTIVATE
    # ========================================================

    def test_deactivate_source(
        self,
    ):
        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Active Source",
            url="https://example.com",
            is_active=True,
        )

        service = KnowledgeSourceService()

        result = service.deactivate_source(
            source=source,
        )

        result.refresh_from_db()

        self.assertFalse(
            result.is_active,
        )

    def test_deactivate_already_inactive_source_is_safe(
        self,
    ):
        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Inactive Source",
            url="https://example.com",
            is_active=False,
        )

        service = KnowledgeSourceService()

        result = service.deactivate_source(
            source=source,
        )

        self.assertFalse(
            result.is_active,
        )

    def test_activate_source(
        self,
    ):
        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Inactive Source",
            url="https://example.com",
            is_active=False,
        )

        service = KnowledgeSourceService()

        result = service.activate_source(
            source=source,
        )

        result.refresh_from_db()

        self.assertTrue(
            result.is_active,
        )

    def test_activate_already_active_source_is_safe(
        self,
    ):
        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Active Source",
            url="https://example.com",
            is_active=True,
        )

        service = KnowledgeSourceService()

        result = service.activate_source(
            source=source,
        )

        self.assertTrue(
            result.is_active,
        )

    # ========================================================
    # VALIDATION
    # ========================================================

    def test_none_source_is_rejected(
        self,
    ):
        service = KnowledgeSourceService()

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.deactivate_source(
                source=None,
            )

    def test_source_without_organization_is_rejected(
        self,
    ):
        service = KnowledgeSourceService()

        source = KnowledgeSource(
            source_type=KnowledgeSource.SourceType.URL,
            name="Invalid",
            url="https://example.com",
        )

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.deactivate_source(
                source=source,
            )

    # ========================================================
    # ORGANIZATION ISOLATION
    # ========================================================

    def test_sources_are_organization_scoped(
        self,
    ):
        service = KnowledgeSourceService()

        first = service.create_url_source(
            organization=self.organization,
            url="https://example.com/a",
            name="Organization A",
        )

        second = service.create_url_source(
            organization=self.other_organization,
            url="https://example.com/b",
            name="Organization B",
        )

        self.assertNotEqual(
            first.organization_id,
            second.organization_id,
        )

        self.assertEqual(
            KnowledgeSource.objects.filter(
                organization=self.organization,
            ).count(),
            1,
        )

        self.assertEqual(
            KnowledgeSource.objects.filter(
                organization=self.other_organization,
            ).count(),
            1,
        )

    # ========================================================
    # SOURCE VALIDATION
    # ========================================================

    def test_process_url_source_rejects_missing_source(
        self,
    ):
        service = KnowledgeSourceService()

        with self.assertRaises(
            KnowledgeSourceServiceError,
        ):
            service.process_url_source(
                source=None,
            )