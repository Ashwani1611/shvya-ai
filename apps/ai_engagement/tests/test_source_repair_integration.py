"""Approved repairs reuse actual ingestion tasks and the metered provider boundary."""
import io
from unittest.mock import patch

from celery.exceptions import Retry
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.ai_engagement.models import AIKnowledgeRepair, Chunk, KnowledgeSource
from apps.ai_engagement.services.source_repair import (
    SourceRepairError, inspect_document, request_repair, repair_report, dispatch_repair, reconcile_request,
)
from apps.ai_engagement.tasks import reindex_document_embeddings
from apps.ai_engagement.tests import test_knowledge_index_recovery as fixtures
from apps.organizations.models import Organization


class SourceRepairIntegrationTests(TestCase):
    setUp = fixtures.KnowledgeIndexRecoveryTests.setUp
    document = fixtures.KnowledgeIndexRecoveryTests.document
    chunk = fixtures.KnowledgeIndexRecoveryTests.chunk
    vectors = staticmethod(fixtures.KnowledgeIndexRecoveryTests.vectors)
    execute = staticmethod(fixtures.KnowledgeIndexRecoveryTests.execute)

    def prepare(self):
        doc = self.document(status="failed")
        self.chunk(doc)
        plan = inspect_document(organization=self.organization, document_id=doc.pk)
        request = request_repair(organization=self.organization, document_id=doc.pk,
                                 expected_fingerprint=plan["fingerprint"], allow_credits=True)
        return doc, plan, request

    def test_inspection_is_read_only_and_includes_failed_inactive_document(self):
        from apps.ai_engagement.services.recovery_evaluation import preflight
        doc = self.document(status="failed")
        self.chunk(doc)
        plan = inspect_document(organization=self.organization, document_id=doc.pk)
        self.assertEqual(plan["action"], "reindex_missing")
        self.assertFalse(doc.is_active)
        self.assertEqual(AIKnowledgeRepair.objects.count(), 0)
        self.provider.embeddings.create.assert_not_called()
        report = preflight(self.organization)
        self.assertEqual(report["source_counts"]["failed_documents"], 1)
        self.assertIn(doc.pk, report["repair_candidate_document_ids"])
        self.assertNotIn("Onboarding includes", str(plan))

    def test_mutation_requires_exact_reviewed_plan_and_credit_consent(self):
        doc = self.document(status="failed"); self.chunk(doc)
        plan = inspect_document(organization=self.organization, document_id=doc.pk)
        with self.assertRaises(SourceRepairError):
            request_repair(organization=self.organization, document_id=doc.pk, expected_fingerprint=plan["fingerprint"])
        doc.name = "Changed"; doc.save()
        with self.assertRaises(SourceRepairError):
            request_repair(organization=self.organization, document_id=doc.pk, expected_fingerprint=plan["fingerprint"], allow_credits=True)
        self.assertFalse(AIKnowledgeRepair.objects.exists())

    def test_repeated_apply_of_same_reviewed_plan_queues_one_request(self):
        with patch.object(reindex_document_embeddings, "apply_async") as publish:
            with self.captureOnCommitCallbacks(execute=True):
                doc, plan, request = self.prepare()
                duplicate = request_repair(organization=self.organization, document_id=doc.pk,
                    expected_fingerprint=plan["fingerprint"], allow_credits=True)
        self.assertEqual(request.pk, duplicate.pk)
        self.assertEqual(AIKnowledgeRepair.objects.count(), 1)
        publish.assert_called_once()
        self.assertTrue(publish.call_args.kwargs["kwargs"]["only_missing"])
        self.assertEqual(publish.call_args.kwargs["task_id"], str(request.pk))

    def test_repair_uses_existing_indexer_once_and_records_verified_cost(self):
        doc, _, request = self.prepare()
        kwargs = {"document_id": doc.pk, "organization_id": self.organization.pk,
                  "only_missing": True, "repair_request_id": str(request.pk)}
        self.assertEqual(self.execute(reindex_document_embeddings, **kwargs)["status"], "completed")
        duplicate = self.execute(reindex_document_embeddings, **kwargs)
        self.assertEqual(duplicate["reason"], "duplicate_or_stale_repair")
        self.assertEqual(self.provider.embeddings.create.call_count, 1)
        report = repair_report(organization=self.organization, request_id=request.pk)
        self.assertEqual(report["state"], "completed")
        self.assertTrue(report["published"])
        self.assertTrue(report["usage"]["accounting_complete"])
        self.assertEqual(report["usage"]["total_credits"], 1)

    def test_existing_embeddings_are_preserved(self):
        doc, _, request = self.prepare()
        original = doc.chunks.first()
        # Re-plan after an operator-visible content change.
        AIKnowledgeRepair.objects.filter(pk=request.pk).update(state="failed")
        original.embedding = [0.01] * Chunk.EMBEDDING_DIMENSIONS; original.save(update_fields=["embedding"])
        self.chunk(doc)
        plan = inspect_document(organization=self.organization, document_id=doc.pk)
        request = request_repair(organization=self.organization, document_id=doc.pk,
                                 expected_fingerprint=plan["fingerprint"], allow_credits=True)
        self.execute(reindex_document_embeddings, document_id=doc.pk, organization_id=self.organization.pk,
                     only_missing=True, repair_request_id=str(request.pk))
        self.assertEqual(len(self.provider.embeddings.create.call_args.kwargs["input"]), 1)

    def test_worker_rejects_source_changes_before_provider_call(self):
        doc, _, request = self.prepare()
        doc.name = "Edited after approval"; doc.save()
        result = self.execute(reindex_document_embeddings, document_id=doc.pk, organization_id=self.organization.pk,
                              only_missing=True, repair_request_id=str(request.pk))
        self.assertEqual(result["status"], "skipped")
        self.provider.embeddings.create.assert_not_called()
        request.refresh_from_db(); self.assertEqual(request.state, "stale")

    def test_existing_celery_retry_keeps_request_and_document_identity(self):
        doc, _, request = self.prepare()
        self.provider.embeddings.create.side_effect = TimeoutError("provider temporary failure")
        with self.assertRaises(Retry) as retry:
            self.execute(reindex_document_embeddings, document_id=doc.pk, organization_id=self.organization.pk,
                         only_missing=True, repair_request_id=str(request.pk))
        retry_kwargs = retry.exception.sig.kwargs
        self.assertEqual(retry_kwargs["repair_request_id"], str(request.pk))
        self.assertEqual(retry_kwargs["document_id"], doc.pk)
        self.assertTrue(retry_kwargs["only_missing"])
        request.refresh_from_db(); self.assertEqual(request.state, "retrying")
        self.provider.embeddings.create.side_effect = self.vectors
        self.execute(reindex_document_embeddings, retries=1, **retry_kwargs)
        request.refresh_from_db(); self.assertEqual(request.state, "completed")
        report = repair_report(organization=self.organization, request_id=request.pk)
        self.assertEqual(report["usage"]["released"], 1)
        self.assertEqual(report["usage"]["total_credits"], 1)

    def test_foreign_document_cannot_be_planned_or_repaired(self):
        doc, _, request = self.prepare()
        other = Organization.objects.create(name="Other repair tenant")
        with self.assertRaises(SourceRepairError):
            inspect_document(organization=other, document_id=doc.pk)
        with self.assertRaises(SourceRepairError):
            repair_report(organization=other, request_id=request.pk)
        self.provider.embeddings.create.assert_not_called()

    def test_inactive_url_source_and_old_version_are_not_revived(self):
        source = KnowledgeSource.objects.create(organization=self.organization, source_type="url",
                                                url="https://example.com/approved", is_active=False)
        doc = self.document(status="failed", source_key=source.url)
        doc.source_url = source.url; doc.save()
        self.chunk(doc)
        self.assertFalse(inspect_document(organization=self.organization, document_id=doc.pk)["repairable"])
        source.is_active = True; source.save()
        self.document(status="completed", version=2, source_key=source.url, active=True)
        self.assertEqual(inspect_document(organization=self.organization, document_id=doc.pk)["reason"], "superseded_version")

    def test_broker_ambiguity_can_only_redispatch_same_deduplicated_ticket(self):
        doc, _, request = self.prepare()
        with patch.object(reindex_document_embeddings, "apply_async", side_effect=RuntimeError("broker ambiguous")):
            self.assertFalse(dispatch_repair(organization_id=self.organization.pk, request_id=request.pk))
        request.refresh_from_db(); self.assertEqual(request.state, "dispatch_failed")
        self.assertEqual(inspect_document(organization=self.organization, document_id=doc.pk)["reason"], "repair_already_in_progress")
        with patch.object(reindex_document_embeddings, "apply_async") as publish:
            self.assertTrue(dispatch_repair(organization_id=self.organization.pk, request_id=request.pk))
            self.assertEqual(publish.call_args.kwargs["task_id"], str(request.pk))

    def test_reconciliation_confirms_persisted_work_without_replaying_provider(self):
        doc, _, request = self.prepare()
        self.execute(reindex_document_embeddings, document_id=doc.pk, organization_id=self.organization.pk,
                     only_missing=True, repair_request_id=str(request.pk))
        AIKnowledgeRepair.objects.filter(pk=request.pk).update(state="uncertain")
        calls = self.provider.embeddings.create.call_count
        report = reconcile_request(organization=self.organization, request_id=request.pk)
        self.assertEqual(report["state"], "completed")
        self.assertFalse(report["usage"]["accounting_complete"])
        self.assertEqual(self.provider.embeddings.create.call_count, calls)

    def test_default_command_is_dry_run_and_apply_requires_fingerprint(self):
        doc = self.document(status="failed"); self.chunk(doc)
        out = io.StringIO()
        call_command("repair_ai_sources", organization_id=self.organization.pk, document_id=doc.pk, stdout=out)
        self.assertFalse(AIKnowledgeRepair.objects.exists())
        with self.assertRaises(CommandError):
            call_command("repair_ai_sources", organization_id=self.organization.pk, document_id=doc.pk, apply=True, stdout=out)
        self.provider.embeddings.create.assert_not_called()
