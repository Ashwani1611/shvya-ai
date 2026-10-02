"""Django/LangGraph integration checks. No live AI or channel sends."""
import os
from dataclasses import dataclass, field
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.graph import workflow
from apps.ai_engagement.models import Chunk, Document
from apps.ai_engagement.services import evidence_recovery as recovery
from apps.ai_engagement.services.embeddings import EmbeddingError, EmbeddingService
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


@dataclass
class Context:
    organization: dict
    lead: dict = field(default_factory=dict)
    knowledge: list = field(default_factory=list)
    conversation: dict = field(default_factory=lambda: {"messages": []})


class RecoveryGraphTests(SimpleTestCase):
    def run_graph(self, enabled=True):
        organization = SimpleNamespace(id="org-a")
        context = Context({"id": "org-a", "about": "Approved company facts"})
        initial = {
            "started_at": monotonic(), "organization": organization,
            "lead": SimpleNamespace(id="lead-a"),
            "service": SimpleNamespace(KNOWLEDGE_LIMIT=5, _validate_context_scope=Mock()),
            "context": context, "latest_text": "What does DIY cost?", "runtime_policy": {"engagement": {"rules": ["Use Hindi"]}},
        }
        flags = {"AI_BRAIN_RECOVERY_ENABLED": "1" if enabled else "0",
                 "AI_BRAIN_RECOVERY_ORGANIZATION_IDS": "org-a", "AI_BRAIN_RECOVERY_BUDGET_SECONDS": "20"}
        hit = {"chunk_id": "1", "document_id": "1", "content": "DIY price is 2999.", "similarity": 0.9}
        with (
            patch.dict(os.environ, flags),
            patch.object(workflow, "_prepare", return_value={}),
            patch.object(workflow, "_deterministic_extract", return_value={}),
            patch.object(workflow, "_route_turn", return_value={"route": "generate"}),
            patch.object(workflow, "_generate", return_value={"decision": "verified decision"}) as generate,
            patch.object(workflow, "_validate_decision", return_value={}) as validate,
            patch.object(workflow, "check_grounding", return_value={"grounding_approved": True}) as ground,
            patch.object(recovery, "_resolution", return_value=None),
            patch.object(recovery, "_sources", return_value=[{"source_id": "faq:1", "content": "Facts"}]),
            patch.object(recovery, "_assess", side_effect=[recovery.Coverage("insufficient", retry_query="DIY price"), recovery.Coverage("sufficient", ("faq:1",), part_count=1, supported_count=1)]) as assess,
            patch.object(recovery, "_search", return_value=([hit], "matched", "ok")) as search,
            patch.object(recovery, "_trace"),
        ):
            result = workflow.build_engagement_graph().invoke(initial)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(ground.call_count, 1)
        self.assertEqual(result["decision"], "verified decision")
        return result, assess, search

    def test_compiled_graph_retries_once_then_keeps_final_gates(self):
        result, assess, search = self.run_graph()
        self.assertEqual(assess.call_count, 2)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(result["retrieval_retries"], 1)
        self.assertEqual(result["runtime_policy"]["engagement"]["rules"], ["Use Hindi"])
        self.assertTrue(result["runtime_policy"]["knowledge_recovery"]["advisory_only"])

    def test_disabled_feature_has_no_extra_ai_or_search_calls(self):
        result, assess, search = self.run_graph(enabled=False)
        assess.assert_not_called()
        search.assert_not_called()
        self.assertNotIn("evidence_coverage", result)

    def test_context_scope_failure_prevents_retrieval(self):
        state = {"organization": SimpleNamespace(id="org-a"), "lead": SimpleNamespace(id="other-lead"),
                 "context": Context({"id": "org-a"}), "retrieval_query": "price",
                 "service": SimpleNamespace(_validate_context_scope=Mock(side_effect=ValueError("wrong scope")))}
        with patch.object(recovery, "_search") as search:
            with self.assertRaises(ValueError):
                recovery.retrieve_evidence(state)
        search.assert_not_called()

    def test_sensitive_evidence_does_not_include_unapproved_about(self):
        state = {"organization": SimpleNamespace(id="org-a"),
                 "context": Context({"about": "UNAPPROVED_PRICE"})}
        resolution = SimpleNamespace(sensitive=True, evidence=[])
        self.assertEqual(recovery._sources(state, resolution), [])

    def test_post_commit_wording_pass_does_not_repeat_recovery(self):
        from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY
        state = {"organization": SimpleNamespace(id="org-a"), "latest_text": "What is the price?"}
        with patch.dict(os.environ, {"AI_BRAIN_RECOVERY_ENABLED": "1", "AI_BRAIN_RECOVERY_ORGANIZATION_IDS": "org-a"}):
            token = _FINAL_LANGUAGE_ONLY.set(True)
            try:
                self.assertEqual(recovery.assess_evidence(state), {})
            finally:
                _FINAL_LANGUAGE_ONLY.reset(token)

    def test_embedding_query_timeout_disables_sdk_retries(self):
        with patch("apps.ai_engagement.services.embeddings.OpenAI") as client:
            EmbeddingService(api_key="test-only", timeout_seconds=5)._get_client()
        client.assert_called_once_with(api_key="test-only", timeout=5.0, max_retries=0)

    def test_ingestion_embedding_defaults_are_unchanged(self):
        with patch("apps.ai_engagement.services.embeddings.OpenAI") as client:
            EmbeddingService(api_key="test-only")._get_client()
        client.assert_called_once_with(api_key="test-only")

    def test_invalid_embedding_timeouts_are_rejected(self):
        for value in (0, -1, 61, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                EmbeddingService(timeout_seconds=value)


class BrainSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Recovery organization")
        cls.other = Organization.objects.create(name="Other recovery tenant")
        cls.pipeline = Pipeline.objects.create(organization=cls.organization, name="Sales", country_code="+91", phone_number="9000000841")
        cls.lead = Lead.objects.create(organization=cls.organization, pipeline=cls.pipeline,
                                      stage=cls.pipeline.stages.get(name="New leads"), phone="+919000000842", name="Customer")

    def state(self):
        return {"organization": self.organization, "lead": self.lead, "started_at": monotonic(),
                "service": SimpleNamespace(KNOWLEDGE_LIMIT=5)}

    def source(self, organization=None, status="completed", content=None):
        document = Document.objects.create(organization=organization or self.organization, name="DIY price", processing_status=status)
        if content is not None:
            Chunk.objects.create(organization=organization or self.organization, document=document, content=content)
        return document

    def test_no_documents_does_not_call_embedding(self):
        with patch.object(EmbeddingService, "embed_text") as embed:
            _, status, _ = recovery._search(self.state(), "DIY price")
        self.assertEqual(status, "no_documents")
        embed.assert_not_called()

    def test_other_tenant_sources_are_invisible(self):
        self.source(organization=self.other, content="DIY price is OTHER_TENANT_VALUE")
        with patch.object(EmbeddingService, "embed_text") as embed:
            chunks, status, _ = recovery._search(self.state(), "DIY price")
        self.assertEqual((chunks, status), ([], "no_documents"))
        embed.assert_not_called()

    def test_failed_source_is_not_a_search_miss(self):
        self.source(status="failed")
        self.assertEqual(recovery._search(self.state(), "DIY price")[1], "source_failed")

    def test_pending_source_is_not_a_search_miss(self):
        self.source(status="pending")
        self.assertEqual(recovery._search(self.state(), "DIY price")[1], "source_not_ready")

    def test_completed_document_without_chunks_is_index_empty(self):
        self.source()
        self.assertEqual(recovery._search(self.state(), "DIY price")[1], "index_empty")

    def test_embedding_failure_can_still_use_keyword_evidence(self):
        self.source(content="DIY price is 2999 per seat.")
        with patch.object(EmbeddingService, "embed_text", side_effect=EmbeddingError("provider unavailable")):
            chunks, status, embedding = recovery._search(self.state(), "DIY price")
        self.assertEqual((status, embedding), ("matched", "embedding_error"))
        self.assertIn("2999", chunks[0]["content"])

    def test_embedding_failure_with_no_keyword_hit_is_not_missing_knowledge(self):
        self.source(content="DIY price is 2999 per seat.")
        with patch.object(EmbeddingService, "embed_text", side_effect=EmbeddingError("provider unavailable")):
            chunks, status, _ = recovery._search(self.state(), "unrelated terminology")
        self.assertEqual((chunks, status), ([], "embedding_error"))

    def test_storage_error_is_content_free(self):
        with patch.object(recovery, "_source_state", side_effect=DatabaseError("private connection information")):
            result = recovery._search(self.state(), "DIY price")
        self.assertEqual(result, ([], "storage_error", ""))
        self.assertNotIn("private", str(result))

    def test_timeout_is_distinct(self):
        self.source(content="DIY price is 2999 per seat.")
        error = EmbeddingError("timed out")
        error.__cause__ = TimeoutError()
        with patch.object(EmbeddingService, "embed_text", side_effect=error):
            _, status, embedding = recovery._search(self.state(), "unrelated terminology")
        self.assertEqual((status, embedding), ("timeout", "timeout"))
