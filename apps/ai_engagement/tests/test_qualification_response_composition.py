"""Known option answers still need authored language/actions/file composition."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph.workflow import _deterministic_extract, _generate, _route_turn
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.engagement import EngagementDecision, EngagementService
from apps.organizations.models import Organization


class QualificationResponseCompositionTests(SimpleTestCase):
    def state(self, *, languages="", playbook="", candidates=None, definitions=None):
        organization = {"id": "org", "bot_languages": languages, "ai_playbook": playbook}
        if candidates is not None:
            organization["_file_candidates"] = candidates
        context = AIContext(
            organization=organization, lead={"id": "lead"},
            pipeline={"attribute_definitions": definitions or []}, stage={},
            contacts=[], attributes=[], knowledge=[], conversation_summary=None,
            qualification_notes=[], conversation={"messages": [
                {"id": "source", "direction": "inbound", "body": "A"}]},
        )
        requirement = {"id": "budget", "question": "What is your budget?", "can_direct_ask": True}
        self.extracted = {
            "changed": True, "answer_status": "answered", "next_requirement": requirement,
            "state": {"engagement_mode": "qualification", "qualification_status": "in_progress",
                      "requirement_states": {"industry": {"status": "answered", "value": "Travel"}}},
        }
        return {"context": context, "organization": SimpleNamespace(id="org"),
                "lead": SimpleNamespace(id="lead"), "requirements": [requirement],
                "latest_message_id": "source", "latest_text": "A", "service": EngagementService()}

    def extract(self, state):
        with patch("apps.ai_engagement.graph.workflow.apply_unambiguous_reply",
                   return_value=self.extracted) as extraction:
            result = _deterministic_extract(state)
        extraction.assert_called_once()
        self.assertEqual(result["qualification_state"], self.extracted["state"])
        return result

    def test_configured_language_retains_extraction_and_routes_to_generation(self):
        state = self.state(languages="Hindi")
        updates = self.extract(state)
        self.assertNotIn("direct_decision", updates)
        self.assertEqual(_route_turn({**state, **updates})["route"], "generate")

    def test_source_specific_playbook_and_attribute_mapping_require_composition(self):
        for settings in (
            {"playbook": "## Rules\nFor Instagram leads acknowledge in Hinglish."},
            {"playbook": "## Attribute mapping logic\nSave the industry response."},
            {"definitions": [{"key": "industry", "name": "Industry", "field_type": "text"}]},
        ):
            with self.subTest(settings=settings):
                state = self.state(**settings)
                self.assertNotIn("direct_decision", self.extract(state))

    def test_short_option_answer_can_select_a_guided_file(self):
        candidate = {"document_id": 7, "share_instruction": "Send after the lead chooses Travel."}
        state = self.state()
        # Candidate discovery must happen even when the initial context had none.
        state["organization"] = Organization(name="Travel")
        with patch("apps.ai_engagement.services.file_sharing.FileSharingService.build_file_candidates",
                   return_value=[candidate]) as files:
            updates = self.extract(state)
            self.assertNotIn("direct_decision", updates)
            self.assertEqual(updates["context"].organization["_file_candidates"], [candidate])
            decision = EngagementDecision(should_engage=True, message="Here is the travel guide.",
                file_document_id=7, crm_actions=[], reason="NORMAL_CONVERSATION", model="test")
            compose = Mock(return_value=decision)
            result = _generate({**state, **updates, "legacy_engage": compose})
        files.assert_called_once()
        self.assertEqual(result["decision"].file_document_id, 7)
        self.assertEqual(compose.call_args.kwargs["context"].organization["_file_candidates"], [candidate])

    def test_unconfigured_no_file_turn_keeps_cheap_backend_question(self):
        state = self.state(candidates=[])
        updates = self.extract(state)
        decision = updates["direct_decision"]
        self.assertEqual(decision.message, "What is your budget?")
        self.assertEqual(_route_turn({**state, **updates})["route"], "direct")
