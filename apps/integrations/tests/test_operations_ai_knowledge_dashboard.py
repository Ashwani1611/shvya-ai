"""MCP Playbooks parity uses real models and canonical publication services."""

import base64
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import override_settings

from apps.ai_engagement.models import Chunk, Document, KnowledgeRepairRequest
from apps.integrations.models import OperationsAuditEvent, OperationsPolicy
from apps.integrations.operations_policy import CAP_AI_CONFIG_WRITE, CAP_ORGANIZATION_READ, ROLE_ORGANIZATION_ADMIN
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


class OperationsAIKnowledgeDashboardTests(OperationsMCPBase):
    def setUp(self):
        super().setUp()
        self.storage = tempfile.TemporaryDirectory(prefix="mcp-knowledge-")
        self.addCleanup(self.storage.cleanup)
        settings = override_settings(MEDIA_ROOT=self.storage.name, STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
        settings.enable()
        self.addCleanup(settings.disable)
        OperationsPolicy.objects.create(organization=self.organization, organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ, CAP_AI_CONFIG_WRITE], approval_required_capabilities=[CAP_AI_CONFIG_WRITE])
        self.bearer = self._token(actor=self.admin, role=ROLE_ORGANIZATION_ADMIN, organization=self.organization)
        self.reason = "Apply the reviewed Playbooks knowledge and sharing configuration."
        self.data = {"name": "Client Services", "filename": "client-services.txt", "content": "We offer weekday customer support and on-site installation.", "ingest": False}

    def _ok(self, name, arguments=None):
        result = self._result(self._call(self.bearer, name, arguments))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _error(self, name, arguments):
        result = self._result(self._call(self.bearer, name, arguments))
        self.assertTrue(result["isError"], result)
        return result

    def _approved(self, name, arguments):
        preview = self._ok(name, {**arguments, "dry_run": True})
        return self._ok(name, {**arguments, "dry_run": False, "approved": True, "approval_event_id": preview["approval_event_id"]})

    def _document(self, **data):
        result = self._approved("create_playbook_document", {"data": {**self.data, **data}, "reason": self.reason})
        return Document.objects.get(pk=result["document"]["id"])

    def test_authored_document_uses_versioned_native_file_source_and_starts_unpublished(self):
        args = {"data": self.data, "reason": self.reason}
        preview = self._ok("create_playbook_document", args)
        self.assertEqual(preview["next_version"], 1)
        self.assertFalse(Document.objects.exists())
        self._error("create_playbook_document", {**args, "dry_run": False})
        document = self._document()
        self.assertFalse(document.is_active)
        self.assertEqual(document.processing_status, "pending")
        with document.file.open("rb") as stored:
            self.assertEqual(stored.read().decode(), self.data["content"])
        self.assertEqual(self._document().version, 2)

    def test_new_source_version_invalidates_approved_creation(self):
        args = {"data": self.data, "reason": self.reason}
        preview = self._ok("create_playbook_document", args)
        self._document()
        self._error("create_playbook_document", {**args, "dry_run": False, "approved": True,
                                                 "approval_event_id": preview["approval_event_id"]})
        self.assertEqual(Document.objects.count(), 1)

    def test_upload_propagates_file_sharing_instruction_and_exact_bytes(self):
        raw = b"Our client brochure covers services and locations."
        data = {"name": "Brochure", "filename": "brochure.txt", "content_base64": base64.b64encode(raw).decode(),
                "share_instruction": "Send with the welcome message when the customer asks about services."}
        args = {"data": data, "reason": self.reason}
        preview = self._ok("upload_knowledge_document", args)
        self.assertTrue(preview["guided_sharing_enabled_on_save"])
        self.assertNotIn(data["content_base64"], json.dumps(preview))
        with patch("apps.ai_engagement.tasks.ingest_and_index_document.delay") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                result = self._ok("upload_knowledge_document", {**args, "dry_run": False, "approved": True,
                                                                 "approval_event_id": preview["approval_event_id"]})
            enqueue.assert_called_once()
        self.assertTrue(result["ingestion_scheduled"])
        self.assertFalse(result["ingestion_queued"])
        self.assertEqual(result["dispatch_status"], "pending_commit")
        self.assertTrue(result["readback_required"])
        document = Document.objects.get(pk=result["document"]["id"])
        self.assertEqual(document.share_instruction, data["share_instruction"])
        self.assertTrue(document.file_sharing_ready)
        self.assertFalse(document.is_active)

    def test_upload_rejects_invalid_file_before_preview_or_save(self):
        data = {"filename": "invalid.pdf", "content_base64": base64.b64encode(b"not a valid PDF").decode()}
        self._error("upload_knowledge_document", {"data": data, "reason": self.reason})
        self.assertFalse(Document.objects.exists())
        self._error("create_playbook_document", {"data": {**self.data, "filename": "../unsafe.txt"}, "reason": self.reason})

    def test_chunk_reads_are_tenant_scoped_bounded_and_do_not_expose_embeddings(self):
        document = self._document()
        Chunk.objects.create(document=document, organization=self.organization, content="Evidence paragraph " * 100, chunk_index=0)
        Chunk.objects.create(document=document, organization=self.organization, content="Next paragraph", chunk_index=1)
        Chunk.objects.create(document=document, organization=self.other_organization, content="Foreign tenant secret source", chunk_index=2)
        result = self._ok("get_playbook_document", {"document_id": document.pk, "chunk_limit": 1})
        self.assertTrue(result["document"]["chunk_scope_mismatch"])
        self.assertFalse(result["document"]["retrieval_ready"])
        self.assertEqual(result["next_chunk_offset"], 1)
        self.assertEqual(result["chunks"][0]["content"], "Evidence paragraph " * 100)
        second = self._ok("get_playbook_document", {"document_id": document.pk, "chunk_offset": 1, "chunk_limit": 1})
        self.assertIsNone(second["next_chunk_offset"])
        self.assertNotIn("Foreign tenant secret source", json.dumps(second))
        self.assertNotIn("embedding", result["chunks"][0])
        foreign = Document.objects.create(organization=self.other_organization, name="Foreign", source_key="foreign", version=1)
        self._error("get_playbook_document", {"document_id": foreign.pk})

    def test_document_list_filters_and_paginates_without_storage_urls(self):
        first = self._document()
        second = self._document(filename="second.txt", name="Another", share_instruction="Share when requested.")
        result = self._ok("list_playbook_documents", {"limit": 1})
        self.assertEqual(result["documents"][0]["id"], first.pk)
        page = self._ok("list_playbook_documents", {"limit": 1, "cursor": result["next_cursor"]})
        self.assertEqual(page["documents"][0]["id"], second.pk)
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(self._ok("list_playbook_documents", {"guided_only": True})["count"], 1)
        self.assertNotIn(first.file.name, json.dumps(result))

    def test_metadata_update_validates_guided_file_and_clears_readiness(self):
        document = self._document()
        instruction = "Send this file after the customer requests installation options."
        result = self._approved("update_knowledge_document", {"document_id": document.pk,
            "changes": {"name": "Installation Guide", "share_instruction": instruction}, "reason": self.reason})
        self.assertTrue(result["document"]["file_sharing_ready"])
        self.assertEqual(result["document"]["share_instruction"], instruction)
        result = self._approved("update_knowledge_document", {"document_id": document.pk,
            "changes": {"share_instruction": ""}, "reason": self.reason})
        self.assertFalse(result["document"]["file_sharing_ready"])
        self.assertFalse(result["messages_sent"])

    def test_metadata_approval_detects_drift_and_file_validation_rolls_back(self):
        document = self._document()
        args = {"document_id": document.pk, "changes": {"name": "New name"}, "reason": self.reason}
        preview = self._ok("update_knowledge_document", args)
        Document.objects.filter(pk=document.pk).update(share_instruction="Concurrent instruction")
        self._error("update_knowledge_document", {**args, "dry_run": False, "approved": True,
                                                  "approval_event_id": preview["approval_event_id"]})
        document.refresh_from_db()
        self.assertEqual(document.name, self.data["name"])
        Path(document.file.path).write_bytes(b"\x00\x01\x02")
        self._error("update_knowledge_document", {"document_id": document.pk,
            "changes": {"share_instruction": "Send a guide."}, "reason": self.reason})
        document.refresh_from_db()
        self.assertEqual(document.share_instruction, "Concurrent instruction")

    def test_retry_creates_durable_canonical_repair_without_premature_completion(self):
        document = self._document()
        Document.objects.filter(pk=document.pk).update(processing_status="failed", is_active=False)
        args = {"document_id": document.pk, "reason": self.reason, "operation": "retry_upload"}
        preview = self._ok("repair_knowledge_document", args)
        self.assertEqual(preview["plan"]["operation"], "retry_upload")
        self.assertFalse(KnowledgeRepairRequest.objects.exists())
        with patch("apps.ai_engagement.tasks.ingest_and_index_document.apply_async") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                result = self._ok("repair_knowledge_document", {**args, "dry_run": False, "approved": True,
                                                                 "approval_event_id": preview["approval_event_id"]})
            enqueue.assert_called_once()
        self.assertEqual(result["status"], "REQUESTED")
        self.assertFalse(result["processing_completed"])
        self.assertEqual(KnowledgeRepairRequest.objects.get().document_id, document.pk)

    def test_reindex_uses_missing_only_canonical_plan_and_blocks_drift(self):
        document = self._document()
        Document.objects.filter(pk=document.pk).update(processing_status="completed", is_active=True)
        chunk = Chunk.objects.create(document=document, organization=self.organization, content="Current evidence")
        args = {"document_id": document.pk, "reason": self.reason}
        preview = self._ok("repair_knowledge_document", args)
        self.assertEqual(preview["plan"]["operation"], "reindex_missing")
        Chunk.objects.filter(pk=chunk.pk).update(content="New evidence invalidates the preview")
        self._error("repair_knowledge_document", {**args, "dry_run": False, "approved": True,
                                                  "approval_event_id": preview["approval_event_id"]})
        self.assertFalse(KnowledgeRepairRequest.objects.exists())
        with patch("apps.ai_engagement.tasks.reindex_document_embeddings.apply_async") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                self._approved("repair_knowledge_document", args)
            self.assertTrue(enqueue.call_args.kwargs["kwargs"]["only_missing"])

    def test_superseded_and_foreign_documents_cannot_be_repaired(self):
        old = self._document()
        self._document()
        Document.objects.filter(pk=old.pk).update(processing_status="failed")
        args = {"document_id": old.pk, "reason": self.reason}
        preview = self._ok("repair_knowledge_document", args)
        self.assertFalse(preview["repair_available"])
        self.assertEqual(preview["plan"]["reason"], "superseded_document")
        self._error("repair_knowledge_document", {**args, "dry_run": False, "approved": True,
                                                  "approval_event_id": preview["approval_event_id"]})
        other = Document.objects.create(organization=self.other_organization, name="Other", processing_status="failed")
        self._error("repair_knowledge_document", {"document_id": other.pk, "reason": self.reason})

    def test_failed_broker_publication_preserves_upload_and_allows_retry(self):
        with patch("apps.ai_engagement.tasks.ingest_and_index_document.delay", side_effect=RuntimeError("offline")):
            with self.captureOnCommitCallbacks(execute=True):
                document = self._document(ingest=True)
        document.refresh_from_db()
        self.assertEqual(document.processing_status, "failed")
        self.assertTrue(document.file.storage.exists(document.file.name))
        preview = self._ok("repair_knowledge_document", {"document_id": document.pk, "reason": self.reason})
        self.assertTrue(preview["repair_available"])

    def test_source_content_and_credentials_are_absent_from_audit(self):
        document = self._document()
        Chunk.objects.create(document=document, organization=self.organization, content="password=private-source-secret")
        result = self._ok("get_playbook_document", {"document_id": document.pk})
        self.assertNotIn("private-source-secret", json.dumps(result))
        for event in OperationsAuditEvent.objects.filter(tool_name__in=["create_playbook_document", "get_playbook_document"]):
            encoded = json.dumps(event.change_summary)
            self.assertNotIn(self.data["content"], encoded)
            self.assertNotIn(document.file.name, encoded)
