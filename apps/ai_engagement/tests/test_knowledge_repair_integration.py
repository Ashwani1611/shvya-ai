"""Explicit revision-bound repairs reuse canonical tasks and do not send messages."""
import io
import json
from unittest.mock import patch

from celery.exceptions import Retry
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.ai_engagement.models import Chunk, Document, KnowledgeRepairRequest, KnowledgeSource
from apps.ai_engagement.services.knowledge_repair import (
    KnowledgeRepairError, plan_repair, request_repair, dispatch_repair,
)
from apps.ai_engagement.services.recovery_evaluation import preflight
from apps.ai_engagement.services.credits import AICreditService
from apps.ai_engagement.tasks import reindex_document_embeddings
from apps.ai_engagement.tests import test_knowledge_index_recovery as fixtures
from apps.organizations.models import Organization


class KnowledgeRepairIntegrationTests(TestCase):
    setUp = fixtures.KnowledgeIndexRecoveryTests.setUp
    vectors = staticmethod(fixtures.KnowledgeIndexRecoveryTests.vectors)
    document = fixtures.KnowledgeIndexRecoveryTests.document
    chunk = fixtures.KnowledgeIndexRecoveryTests.chunk

    def failed_document(self, **kwargs):
        document = self.document(status="failed", active=False, **kwargs)
        self.chunk(document)
        return document

    def request(self, document, *, broker_error=None):
        plan = plan_repair(organization=self.organization, document_id=document.pk)
        with patch("apps.ai_engagement.tasks.reindex_document_embeddings.apply_async", side_effect=broker_error) as send:
            with self.captureOnCommitCallbacks(execute=True):
                result = request_repair(organization=self.organization, document_id=document.pk,
                    expected_fingerprint=plan["fingerprint"])
        result.refresh_from_db()
        return result, send

    def run_request(self, request, *, retries=0, **overrides):
        kwargs = {"document_id": request.document_id, "organization_id": str(request.organization_id), "only_missing": True}
        kwargs.update(overrides)
        task = reindex_document_embeddings
        task.push_request(id=str(request.task_id), retries=retries, called_directly=False, is_eager=True, args=(), kwargs=kwargs)
        try:
            return task.run(**kwargs)
        finally:
            task.pop_request()

    def test_dry_run_is_read_only_and_redacted(self):
        document = self.failed_document()
        before = KnowledgeRepairRequest.objects.count()
        output = io.StringIO()
        call_command("repair_ai_knowledge", organization_id=self.organization.pk, document_id=document.pk, stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["operation"], "reindex_missing")
        self.assertEqual(report["missing_embedding_count"], 1)
        self.assertEqual(KnowledgeRepairRequest.objects.count(), before)
        self.provider.embeddings.create.assert_not_called()
        self.assertNotIn("Onboarding", output.getvalue())

    def test_preflight_surfaces_inactive_failed_sources_for_repair(self):
        document = self.failed_document()
        report = preflight(self.organization)
        self.assertEqual(report["source_counts"]["failed_documents"], 1)
        self.assertIn(document.pk, report["repair_document_ids"])
        self.assertIn("repair_ai_knowledge", report["repair_command"])

    def test_foreign_document_is_not_available(self):
        document = self.failed_document()
        other = Organization.objects.create(name="Other repair tenant")
        with self.assertRaisesRegex(KnowledgeRepairError, "document_not_found"):
            plan_repair(organization=other, document_id=document.pk)

    def test_explicit_apply_requires_exact_dry_run_fingerprint(self):
        document = self.failed_document()
        with self.assertRaises(CommandError):
            call_command("repair_ai_knowledge", organization_id=self.organization.pk,
                document_id=document.pk, apply=True, stdout=io.StringIO())
        with self.assertRaisesRegex(KnowledgeRepairError, "repair_plan_changed"):
            request_repair(organization=self.organization, document_id=document.pk, expected_fingerprint="incorrect")
        self.assertFalse(KnowledgeRepairRequest.objects.exists())

    def test_apply_is_idempotent_and_dispatches_only_after_commit(self):
        document = self.failed_document()
        plan = plan_repair(organization=self.organization, document_id=document.pk)
        with patch("apps.ai_engagement.tasks.reindex_document_embeddings.apply_async") as send:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                first = request_repair(organization=self.organization, document_id=document.pk,
                    expected_fingerprint=plan["fingerprint"])
                second = request_repair(organization=self.organization, document_id=document.pk,
                    expected_fingerprint=plan["fingerprint"])
                send.assert_not_called()
            self.assertEqual(len(callbacks), 1)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["task_id"], str(first.task_id))
        self.assertTrue(send.call_args.kwargs["kwargs"]["only_missing"])

    def test_broker_failure_can_redispatch_same_canonical_task_identity(self):
        request, _ = self.request(self.failed_document(), broker_error=RuntimeError("broker unavailable"))
        self.assertEqual(request.status, "dispatch_failed")
        with patch("apps.ai_engagement.tasks.reindex_document_embeddings.apply_async") as send:
            self.assertEqual(dispatch_repair(organization_id=self.organization.pk, request_id=request.pk), "queued")
        self.assertEqual(send.call_args.kwargs["task_id"], str(request.task_id))
        self.assertNotIn("broker unavailable", request.outcome_code)

    def test_real_task_repairs_only_missing_embeddings_and_publishes(self):
        document = self.failed_document()
        already = Chunk.objects.create(organization=self.organization, document=document,
            chunk_index=1, content="Already indexed approved content.", embedding=[0.02] * Chunk.EMBEDDING_DIMENSIONS)
        request, _ = self.request(document)
        result = self.run_request(request)
        self.assertEqual(result["status"], "completed")
        request.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(request.status, "succeeded")
        self.assertEqual(request.outcome_code, "published_index_verified")
        self.assertTrue(document.is_active)
        self.assertEqual(document.processing_status, "completed")
        self.assertEqual(self.provider.embeddings.create.call_count, 1)
        self.assertEqual(len(self.provider.embeddings.create.call_args.kwargs["input"]), 1)
        already.refresh_from_db()
        self.assertAlmostEqual(float(already.embedding[0]), 0.02)

    def test_duplicate_delivery_does_not_repeat_successful_provider_work(self):
        request, _ = self.request(self.failed_document())
        self.run_request(request)
        first_count = self.provider.embeddings.create.call_count
        result = self.run_request(request)
        self.assertEqual(result["reason"], "repair_already_claimed_or_finished")
        self.assertEqual(self.provider.embeddings.create.call_count, first_count)

    def test_changed_content_is_rejected_before_calling_provider(self):
        document = self.failed_document()
        request, _ = self.request(document)
        document.chunks.update(content="Different approved content now.")
        result = self.run_request(request)
        self.assertEqual(result["reason"], "repair_plan_changed")
        request.refresh_from_db()
        self.assertEqual(request.status, "skipped")
        self.provider.embeddings.create.assert_not_called()

    def test_task_scope_is_rechecked_not_trusted_from_broker_arguments(self):
        request, _ = self.request(self.failed_document())
        other = Organization.objects.create(name="Other broker tenant")
        with self.assertRaisesRegex(KnowledgeRepairError, "scope_mismatch"):
            self.run_request(request, organization_id=str(other.pk))
        self.provider.embeddings.create.assert_not_called()

    def test_canonical_transient_retry_keeps_request_identity(self):
        request, _ = self.request(self.failed_document())
        self.provider.embeddings.create.side_effect = TimeoutError("transient")
        with self.assertRaises(Retry):
            self.run_request(request)
        request.refresh_from_db()
        self.assertEqual(request.status, "retrying")
        first_calls = self.provider.embeddings.create.call_count
        stale = self.run_request(request)
        self.assertEqual(stale["reason"], "stale_repair_attempt")
        self.assertEqual(self.provider.embeddings.create.call_count, first_calls)
        self.provider.embeddings.create.side_effect = self.vectors
        self.run_request(request, retries=1)
        request.refresh_from_db()
        self.assertEqual(request.status, "succeeded")
        self.assertEqual(request.attempt, 2)
        self.assertEqual(AICreditService.ensure_wallet(self.organization).reserved_credits, 0)

    def test_retired_and_superseded_documents_are_not_reactivated(self):
        inactive = self.document(status="completed", active=False)
        self.chunk(inactive)
        self.assertIsNone(plan_repair(organization=self.organization, document_id=inactive.pk)["operation"])
        Document.objects.create(organization=self.organization, name="New", source_key=inactive.source_key,
            version=2, processing_status="completed", is_active=True)
        self.assertEqual(plan_repair(organization=self.organization, document_id=inactive.pk)["reason"], "superseded_document")

    def test_disabled_url_source_is_not_repaired(self):
        document = self.failed_document(source_key="https://example.com/guide")
        document.source_url = document.source_key
        document.save()
        KnowledgeSource.objects.create(organization=self.organization, source_type="url", url=document.source_key,
            is_active=False)
        self.assertEqual(plan_repair(organization=self.organization, document_id=document.pk)["reason"], "source_disabled_or_missing")

    def test_source_disabled_during_embedding_cannot_be_republished(self):
        document = self.failed_document(source_key="https://example.com/guide")
        document.source_url = document.source_key
        document.save()
        source = KnowledgeSource.objects.create(organization=self.organization, source_type="url", url=document.source_key)
        request, _ = self.request(document)
        def deactivate(**kwargs):
            KnowledgeSource.objects.filter(pk=source.pk).update(is_active=False)
            return self.vectors(**kwargs)
        self.provider.embeddings.create.side_effect = deactivate
        self.run_request(request)
        request.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(request.status, "skipped")
        self.assertEqual(request.outcome_code, "result_not_published")
        self.assertFalse(document.is_active)

    def test_missing_extracted_file_content_uses_existing_ingestion_task(self):
        document = self.document(status="failed", active=False, file=True)
        plan = plan_repair(organization=self.organization, document_id=document.pk)
        self.assertEqual(plan["operation"], "retry_upload")
        with patch("apps.ai_engagement.tasks.ingest_and_index_document.apply_async") as send:
            with self.captureOnCommitCallbacks(execute=True):
                request_repair(organization=self.organization, document_id=document.pk,
                    expected_fingerprint=plan["fingerprint"])
        self.assertEqual(send.call_args.kwargs["kwargs"]["document_id"], document.pk)

    def test_missing_url_content_uses_existing_source_task(self):
        source = KnowledgeSource.objects.create(organization=self.organization, source_type="url", url="https://example.com")
        document = Document.objects.create(organization=self.organization, name="URL", source_key=source.url,
            source_url=source.url, processing_status="failed", is_active=False)
        plan = plan_repair(organization=self.organization, document_id=document.pk)
        self.assertEqual(plan["operation"], "refresh_url")
        with patch("apps.ai_engagement.tasks.ingest_and_index_url_source.apply_async") as send:
            with self.captureOnCommitCallbacks(execute=True):
                request_repair(organization=self.organization, document_id=document.pk,
                    expected_fingerprint=plan["fingerprint"])
        self.assertEqual(send.call_args.kwargs["kwargs"]["source_id"], source.pk)

    def test_wrong_tenant_chunk_blocks_repair_without_exposing_content(self):
        document = self.failed_document()
        other = Organization.objects.create(name="Other chunk tenant")
        document.chunks.update(organization=other, content="PRIVATE_OTHER_DATA")
        report = plan_repair(organization=self.organization, document_id=document.pk)
        self.assertEqual(report["reason"], "chunk_tenant_mismatch")
        self.assertNotIn("PRIVATE_OTHER_DATA", json.dumps(report))

    def test_source_deleted_after_queue_is_skipped_before_provider(self):
        document = self.failed_document(source_key="https://example.com/deleted")
        document.source_url = document.source_key
        document.save()
        source = KnowledgeSource.objects.create(organization=self.organization, source_type="url", url=document.source_url)
        request, _ = self.request(document)
        source.delete()
        result = self.run_request(request)
        request.refresh_from_db()
        self.assertEqual(result["reason"], "source_disabled_or_missing")
        self.assertEqual(request.status, "skipped")
        self.provider.embeddings.create.assert_not_called()

    def test_completed_request_does_not_regress_after_source_is_disabled(self):
        document = self.failed_document(source_key="https://example.com/finished")
        document.source_url = document.source_key
        document.save()
        source = KnowledgeSource.objects.create(organization=self.organization, source_type="url", url=document.source_url)
        request, _ = self.request(document)
        self.run_request(request)
        request.refresh_from_db()
        self.assertEqual(request.status, "succeeded")
        calls = self.provider.embeddings.create.call_count
        source.is_active = False
        source.save(update_fields=["is_active"])
        self.assertEqual(self.run_request(request)["reason"], "repair_already_claimed_or_finished")
        request.refresh_from_db()
        self.assertEqual(request.status, "succeeded")
        self.assertEqual(self.provider.embeddings.create.call_count, calls)

    def test_organization_disabled_during_embedding_cannot_be_republished(self):
        document = self.failed_document()
        request, _ = self.request(document)
        def deactivate(**kwargs):
            Organization.objects.filter(pk=self.organization.pk).update(is_active=False)
            return self.vectors(**kwargs)
        self.provider.embeddings.create.side_effect = deactivate
        self.run_request(request)
        request.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(request.status, "skipped")
        self.assertFalse(document.is_active)

    def test_foreign_task_arguments_cannot_mutate_a_disabled_source_request(self):
        document = self.failed_document(source_key="https://example.com/scoped")
        document.source_url = document.source_key
        document.save()
        source = KnowledgeSource.objects.create(organization=self.organization, source_type="url", url=document.source_url)
        request, _ = self.request(document)
        source.delete()
        other = Organization.objects.create(name="Foreign task")
        with self.assertRaisesRegex(KnowledgeRepairError, "scope_mismatch"):
            self.run_request(request, organization_id=str(other.pk))
        request.refresh_from_db()
        self.assertEqual(request.status, "queued")
        self.provider.embeddings.create.assert_not_called()
