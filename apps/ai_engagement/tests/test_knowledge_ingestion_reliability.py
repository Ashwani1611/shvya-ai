from __future__ import annotations

import io
import tempfile
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from docx import Document as DocxDocument

from apps.ai_engagement.models import Chunk, Document, KnowledgeSource
from apps.ai_engagement.services.embedding_index import EmbeddingIndexError, EmbeddingIndexService
from apps.ai_engagement.services.embeddings import EmbeddingError
from apps.ai_engagement.services.knowledge import KnowledgeExtractionError, KnowledgeIngestionService
from apps.ai_engagement.services.knowledge_source import KnowledgeSourceService
from apps.ai_engagement.tests.test_knowledge_url_security import FakeConnection, FakeResponse
from apps.organizations.models import Organization


class LinkedKnowledgeDocumentTests(SimpleTestCase):
    def extract(self, raw, content_type, path="/guide"):
        response = FakeResponse(headers={"Content-Type": content_type}, chunks=[raw])
        connection = FakeConnection()
        with patch(
            "apps.ai_engagement.services.knowledge_url_security._open_pinned_response",
            return_value=(connection, response),
        ):
            result = KnowledgeIngestionService().extract_url_text(
                f"https://93.184.216.34{path}"
            )
        self.assertTrue(response.closed)
        self.assertTrue(connection.closed)
        return result

    def test_csv_link_uses_the_file_extractor(self):
        result = self.extract(b"Plan,Price\nGrowth,2999\n", "text/csv")
        self.assertIn("Growth | 2999", result)

    def test_docx_link_uses_validated_office_parser(self):
        document = DocxDocument()
        document.add_paragraph("Implementation includes migration and training.")
        stream = io.BytesIO()
        document.save(stream)
        result = self.extract(
            stream.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn("migration and training", result)

    def test_generic_download_mime_uses_supported_url_extension(self):
        result = self.extract(
            b"Support is available in English and Hindi.", "application/octet-stream",
            "/support.txt?download=1",
        )
        self.assertIn("English and Hindi", result)

    def test_linked_file_still_requires_valid_file_content(self):
        with self.assertRaisesRegex(KnowledgeExtractionError, "does not match"):
            self.extract(b"This is not a PDF", "application/pdf")
        with self.assertRaisesRegex(KnowledgeExtractionError, "binary null"):
            self.extract(b"malformed\x00text", "text/plain")

    def test_html_encoding_is_detected_before_text_is_decoded(self):
        result = self.extract(
            '<html><meta charset="iso-8859-1"><body>Caf\xe9 services</body></html>'.encode("latin-1"),
            "text/html; charset=iso-8859-1",
        )
        self.assertEqual(result, "Caf\xe9 services")


class KnowledgeImportReliabilityTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.media = override_settings(MEDIA_ROOT=self.folder.name)
        self.media.enable()
        self.addCleanup(self.media.disable)
        self.organization = Organization.objects.create(name="Import reliability")

    def document(self, *, version=1, status="pending", file=False):
        return Document.objects.create(
            organization=self.organization, name="Guide", source_key="guide.txt",
            version=version, processing_status=status, is_active=False,
            file=SimpleUploadedFile("guide.txt", f"Version {version} support terms.".encode())
            if file else "",
        )

    def chunks(self, document, count):
        return Chunk.objects.bulk_create([
            Chunk(organization=self.organization, document=document, chunk_index=index,
                  content=f"Support topic {index} is included.")
            for index in range(count)
        ])

    @staticmethod
    def vectors(texts):
        return [[0.01] * Chunk.EMBEDDING_DIMENSIONS for _ in texts]

    def test_large_import_batches_embeddings_and_resumes_only_missing_chunks(self):
        document = self.document(status="completed")
        chunks = self.chunks(document, 65)
        provider = Mock()
        provider.embed_texts.side_effect = [
            self.vectors([chunk.content for chunk in chunks[:64]]),
            EmbeddingError("Temporary provider error"),
        ]
        indexer = EmbeddingIndexService(embedding_service=provider)
        with self.assertRaises(EmbeddingIndexError):
            indexer.index_document(document)
        self.assertEqual(document.chunks.filter(embedding__isnull=False).count(), 64)
        self.assertEqual([len(call.args[0]) for call in provider.embed_texts.call_args_list], [64, 1])
        provider.reset_mock()
        provider.embed_texts.side_effect = self.vectors
        self.assertEqual(indexer.index_document(document), 1)
        provider.embed_texts.assert_called_once_with([chunks[-1].content])
        self.assertFalse(document.chunks.filter(embedding__isnull=True).exists())

    def test_document_indexing_rejects_foreign_chunks_before_provider_calls(self):
        document = self.document(status="completed")
        other = Organization.objects.create(name="Other organization")
        Chunk.objects.create(organization=other, document=document, content="Private terms.")
        provider = Mock()
        with self.assertRaisesRegex(EmbeddingIndexError, "document organization"):
            EmbeddingIndexService(embedding_service=provider).index_document(document)
        provider.embed_texts.assert_not_called()

    def test_out_of_order_uploads_keep_reserved_versions_and_newest_content(self):
        old = self.document(version=1, file=True)
        new = self.document(version=2, file=True)
        ingestion = KnowledgeIngestionService()
        ingestion.ingest_document(new)
        ingestion.publish_document_version(new)
        ingestion.ingest_document(old)
        ingestion.publish_document_version(old)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.version, 1)
        self.assertEqual(new.version, 2)
        self.assertFalse(old.is_active)
        self.assertTrue(new.is_active)
        self.assertIn("Version 2", new.chunks.get().content)

    def test_url_source_returns_newly_extracted_version_before_publication(self):
        source = KnowledgeSource.objects.create(
            organization=self.organization, source_type="url", name="Website",
            url="https://93.184.216.34/guide",
        )
        service = KnowledgeSourceService()
        with patch.object(service.ingestion_service, "extract_url_text", return_value="First terms."):
            first = service.process_url_source(source=source)
        self.assertFalse(first.is_active)
        service.ingestion_service.publish_document_version(first)
        with patch.object(service.ingestion_service, "extract_url_text", return_value="Current terms."):
            current = service.process_url_source(source=source)
        self.assertNotEqual(first.pk, current.pk)
        self.assertEqual(current.version, 2)
        self.assertFalse(current.is_active)
        self.assertEqual(current.chunks.get().content, "Current terms.")
