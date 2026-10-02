"""Validated attachments do not depend on paid text extraction/indexing."""
from io import StringIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.file_sharing import FileSharingError, FileSharingService
from apps.ai_engagement.services.knowledge_source import KnowledgeSourceService, KnowledgeSourceServiceError
from apps.organizations.models import Organization


class GuidedFileReadinessTests(TestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.org = Organization.objects.create(name="Guided uploads")
        self.service = FileSharingService()

    def upload(self, *, name="guide.txt", instruction="Send when the visitor says hello."):
        return KnowledgeSourceService().create_file_source(
            organization=self.org,
            uploaded_file=SimpleUploadedFile(name, b"Our product guide"),
            share_instruction=instruction,
        )[1]

    def test_valid_upload_is_candidate_before_indexing_and_after_index_failure(self):
        document = self.upload()
        self.assertFalse(document.is_active)
        self.assertTrue(document.file_sharing_ready)
        context = SimpleNamespace(lead={}, as_dict=lambda: {
            "knowledge": [], "lead": {},
            "conversation": {"messages": [{"direction": "inbound", "body": "Hello"}]},
        })
        for status in ("pending", "processing", "failed"):
            with self.subTest(status=status):
                document.processing_status = status
                document.save(update_fields=["processing_status"])
                candidates = self.service.build_file_candidates(organization=self.org, context=context)
                self.assertEqual([item["document_id"] for item in candidates], [document.pk])
                self.assertEqual(candidates[0]["share_instruction"], document.share_instruction)

    def test_knowledge_only_upload_is_not_automatically_shareable(self):
        document = self.upload(instruction="")
        self.assertFalse(document.file_sharing_ready)
        self.assertIsNone(self.service.get_guided_document(organization=self.org, document_id=document.pk))

    def test_invalid_upload_never_becomes_shareable(self):
        with self.assertRaises(KnowledgeSourceServiceError):
            self.upload(name="invalid.pdf")
        self.assertFalse(Document.objects.filter(organization=self.org).exists())

    def test_new_validated_version_replaces_old_candidate_before_indexing(self):
        old = self.upload()
        old.is_active, old.processing_status = True, "completed"
        old.save(update_fields=["is_active", "processing_status"])
        new = self.upload()
        self.assertEqual(new.version, old.version + 1)
        self.assertEqual([d.pk for d in self.service.get_eligible_documents(organization=self.org)], [new.pk])
        self.assertIsNone(self.service.get_guided_document(organization=self.org, document_id=old.pk))

    def test_ready_flag_does_not_bypass_tenant_or_removed_instruction(self):
        document = self.upload()
        foreign = Organization.objects.create(name="Other")
        with self.assertRaises(FileSharingError):
            self.service.get_guided_document(organization=foreign, document_id=document.pk)
        for instruction in ("", " \n "):
            document.share_instruction = instruction
            document.save(update_fields=["share_instruction"])
            self.assertIsNone(self.service.get_guided_document(organization=self.org, document_id=document.pk))

    def test_recovery_validates_bytes_without_publishing_knowledge_or_reviving_retired_files(self):
        document = self.upload()
        document.file_sharing_ready, document.processing_status = False, "failed"
        document.save(update_fields=["file_sharing_ready", "processing_status"])
        retired = self.upload(name="retired.txt")
        retired.file_sharing_ready, retired.processing_status = False, "completed"
        retired.save(update_fields=["file_sharing_ready", "processing_status"])
        missing = Document.objects.create(organization=self.org, name="Missing", file="missing.txt",
            share_instruction="Send on request", processing_status="failed", is_active=False)
        output = StringIO()
        call_command("prepare_guided_files", stdout=output)
        document.refresh_from_db()
        retired.refresh_from_db()
        missing.refresh_from_db()
        self.assertTrue(document.file_sharing_ready)
        self.assertFalse(document.is_active)
        self.assertEqual(document.processing_status, "failed")
        self.assertFalse(retired.file_sharing_ready)
        self.assertFalse(missing.file_sharing_ready)
        self.assertIn("ready=1, invalid_or_missing=1", output.getvalue())
        with self.assertRaises(FileSharingError):
            self.service.prepare_uploaded_file(document=missing)
