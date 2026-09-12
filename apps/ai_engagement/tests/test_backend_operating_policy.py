from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from apps.ai_engagement.services.runtime_state import contract, observe_message, validate_response, state_revision
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.graph.evidence import check_grounding, _safe_unknown_decision
from apps.ai_engagement.services.ai_provider import AITextResult


class BackendPolicyTests(SimpleTestCase):
    def setUp(self):
        self.requirements = compile_qualification_requirements("Do you own a business?\nA. Yes\nB. No\nWhat is your budget?")["requirements"]
        self.state = {"qualification_status": "in_progress", "conversation_mode": "qualifying", "requirement_states": {}}

    def test_completed_workflow_has_no_current_question(self):
        self.state["qualification_status"] = "completed"
        self.assertIsNone(contract(qualification=self.state, requirements=self.requirements)["current_requirement_id"])

    def test_unknown_booking_is_not_confirmed(self):
        runtime = contract(qualification=self.state, requirements=self.requirements, saved={"booking_status": "confirmed"})
        self.assertEqual(runtime["booking_status"], "unverified")

    def test_user_booking_claim_is_not_tool_confirmation(self):
        result = observe_message({}, "I already booked a demo")
        self.assertEqual(result["booking_status"], "user_reported")
        self.assertNotIn("booking_confirmation", result)

    def test_plain_no_does_not_opt_out(self):
        self.assertNotIn("conversation_mode", observe_message({}, "No"))

    def test_pause_resume_preserves_workflow(self):
        paused = observe_message({"conversation_mode": "qualifying", "active_interaction": "owner"}, "not now")
        self.assertEqual(paused["conversation_mode"], "paused")
        self.assertEqual(observe_message(paused, "continue")["conversation_mode"], "qualifying")
        self.assertEqual(paused["active_interaction"], "owner")

    def test_options_must_be_present_in_order(self):
        runtime = contract(qualification=self.state, requirements=self.requirements)
        for text in ["Do you own a business?", "No / Yes"]:
            with self.assertRaises(ValueError):
                validate_response(decision=SimpleNamespace(next_requirement_id=self.requirements[0]["id"], message=text, should_engage=True), runtime=runtime, requirements=self.requirements)
        validate_response(decision=SimpleNamespace(next_requirement_id=self.requirements[0]["id"], message="Do you own a business? Yes / No", should_engage=True), runtime=runtime, requirements=self.requirements)

    def test_model_cannot_select_future_question(self):
        with self.assertRaises(ValueError):
            validate_response(decision=SimpleNamespace(next_requirement_id=self.requirements[1]["id"], message="Budget?", should_engage=True), runtime=contract(qualification=self.state, requirements=self.requirements), requirements=self.requirements)

    def test_revision_changes_with_backend_state(self):
        lead = SimpleNamespace(attributes={}, stage_id="one")
        previous = state_revision(lead)
        lead.attributes["_shvya_ai_runtime"] = {"booking_status": "confirmed"}
        self.assertNotEqual(previous, state_revision(lead))

    def test_duplicate_message_has_no_second_state_transition(self):
        from apps.ai_engagement.tasks import _persist_engagement_answers
        lead = Mock(organization_id="org")
        lead.whatsapp_messages.select_for_update.return_value.get.return_value = SimpleNamespace(raw_payload={"shvya_ai_processing": {"processed": True}})
        self.assertFalse(_persist_engagement_answers(lead, SimpleNamespace(), "message"))

    def test_rejected_response_cannot_execute_proposed_actions(self):
        decision = EngagementDecision(should_engage=True, message="Booked!", file_document_id=None,
            crm_actions=[{"type": "change_stage"}], reason="ACK", model="test",
            qualification_updates=[{"requirement_id": "owner"}], next_requirement_id="owner")
        safe = _safe_unknown_decision(decision)
        self.assertEqual(safe.crm_actions, [])
        self.assertEqual(safe.qualification_updates, [])
        self.assertIsNone(safe.next_requirement_id)

    def test_validation_cannot_be_bypassed_with_reason_code(self):
        decision = EngagementDecision(should_engage=True, message="Your appointment is confirmed", file_document_id=None,
            crm_actions=[], reason="ACK", reason_code="ACK", model="test")
        context = SimpleNamespace(organization={}, lead={}, conversation={}, knowledge=[])
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.return_value = AITextResult(text='{"approved":false,"reason":"unconfirmed"}', model="test")
            result = check_grounding({"decision": decision, "context": context,
                "organization": SimpleNamespace(id="org"), "lead": SimpleNamespace(id="lead")})
        self.assertFalse(result["grounding_approved"])
        provider.return_value.generate_text.assert_called_once()
