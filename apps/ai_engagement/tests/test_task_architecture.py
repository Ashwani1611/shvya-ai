from django.test import SimpleTestCase

from apps.ai_engagement import tasks


class AITaskArchitectureTests(SimpleTestCase):
    def test_public_task_imports_and_celery_names_stay_stable(self):
        expected = {
            "flush_background_enrichment": "ai.flush_background_enrichment",
            "reconcile_credit_settlements": "ai.reconcile_credit_settlements",
            "dispatch_bump_ups": "ai.dispatch_bump_ups",
            "generate_internal_conversation_summary": "ai.generate_internal_conversation_summary",
            "generate_lead_qualification": "ai.generate_lead_qualification",
            "generate_ai_engagement_response": "ai.generate_ai_engagement_response",
            "recover_api_engagement": "ai.recover_api_engagement",
            "ingest_and_index_document": "ai.ingest_and_index_document",
            "ingest_and_index_url_source": "ai.ingest_and_index_url_source",
            "reindex_document_embeddings": "ai.reindex_document_embeddings",
        }
        for attribute, task_name in expected.items():
            with self.subTest(task=attribute):
                self.assertEqual(task_name, getattr(tasks, attribute).name)

    def test_compatibility_helpers_remain_importable(self):
        self.assertTrue(callable(tasks._persist_engagement_answers))
        self.assertTrue(callable(tasks._execute_ai_engagement_response_impl))
