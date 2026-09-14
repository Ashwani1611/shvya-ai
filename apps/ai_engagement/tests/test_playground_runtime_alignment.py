from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class PlaygroundRuntimeAlignmentTests(SimpleTestCase):
    def _source(self) -> str:
        return (
            Path(settings.BASE_DIR)
            / "apps"
            / "ai_engagement"
            / "services"
            / "playground.py"
        ).read_text(encoding="utf-8")

    def test_sandbox_delegates_generation_to_public_engagement_service(self):
        source = self._source()

        self.assertIn("decision = service.engage(", source)
        self.assertIn("context_builder=_SandboxContextBuilder", source.replace("context_builder=context_builder", "context_builder=_SandboxContextBuilder"))
        self.assertNotIn("_generate_provider_text(", source)
        self.assertNotIn("check_grounding(", source)
        self.assertNotIn("ENGAGEMENT_RESPONSE_SCHEMA", source)

    def test_sandbox_knowledge_is_not_retrieved_for_every_message(self):
        source = self._source()

        self.assertIn("if vector is None and not query:", source)
        self.assertIn("return []", source)
        self.assertIn("knowledge_query=knowledge_query", source)
        self.assertIn("query_vector=query_vector", source)

    def test_sandbox_has_grounded_provider_failure_fallback(self):
        source = self._source()

        self.assertIn("build_deterministic_fallback_decision", source)
        self.assertIn("sandbox-safe-fallback", source)
        self.assertIn("I don’t have enough verified information", source)

    def test_sandbox_reuses_live_first_inbound_welcome_helper(self):
        source = self._source()

        self.assertIn("apply_first_inbound_welcome(", source)
        self.assertIn("first_turn=(turn == 1)", source)
