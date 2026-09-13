from types import SimpleNamespace

from django.test import TestCase

from apps.ai_engagement.services.engagement_failsoft import (
    build_deterministic_fallback_decision,
)
from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    mark_in_progress,
    state_for_lead,
)
from apps.ai_engagement.services.runtime_state import STATE_KEY, finalize_runtime
from apps.ai_engagement.tests import test_engagement_controls as controls
from apps.crm.models import Stage


class NaturalConversationRuntimeTests(TestCase):
    setUp = controls.AIEngagementControlTests.setUp
    _inbound = controls.AIEngagementControlTests._inbound

    def _stage(self, name, order):
        return Stage.objects.create(
            pipeline=self.pipeline,
            name=name,
            display_order=order,
            is_active=True,
            ai_on=True,
        )

    def test_in_conversation_keeps_incomplete_qualification_active(self):
        in_conversation = self._stage("In Conversation", 20)
        self.lead.stage = in_conversation
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()

        state = state_for_lead(self.lead)

        self.assertNotEqual(state["qualification_status"], STATUS_COMPLETED)
        self.assertEqual(state["engagement_mode"], MODE_QUALIFICATION)

    def test_mark_in_progress_works_after_new_lead_moves_to_in_conversation(self):
        in_conversation = self._stage("In Conversation", 20)
        self.lead.stage = in_conversation
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()

        state = mark_in_progress(self.lead)

        self.assertEqual(state["qualification_status"], STATUS_IN_PROGRESS)
        self.assertEqual(state["engagement_mode"], MODE_QUALIFICATION)

    def test_failsoft_treats_cool_as_conversation_not_unknown_information(self):
        inbound = self._inbound("wamid-cool")
        inbound.body = "Cool"
        inbound.save(update_fields=["body", "updated_at"])

        decision = build_deterministic_fallback_decision(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=inbound,
        )

        self.assertEqual(decision.reason_code, "NORMAL_CONVERSATION")
        self.assertEqual(decision.message, "Sounds good.")
        self.assertNotIn("verified information", decision.message)

    def test_failsoft_treats_its_alright_as_conversation_not_unknown_information(self):
        inbound = self._inbound("wamid-alright")
        inbound.body = "It’s alright"
        inbound.save(update_fields=["body", "updated_at"])

        decision = build_deterministic_fallback_decision(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=inbound,
        )

        self.assertEqual(decision.reason_code, "NORMAL_CONVERSATION")
        self.assertEqual(decision.message, "Thanks for understanding.")

    def test_failsoft_call_request_creates_real_crm_actions_when_available(self):
        call_requested = self._stage("Call Requested", 20)
        inbound = self._inbound("wamid-call-request")
        inbound.body = "Call me tomorrow at 5 pm"
        inbound.save(update_fields=["body", "updated_at"])

        decision = build_deterministic_fallback_decision(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=inbound,
        )

        transition = next(
            action
            for action in decision.crm_actions
            if action.get("type") == "pipeline_transition"
        )
        reminder = next(
            action
            for action in decision.crm_actions
            if action.get("type") == "create_reminder"
        )
        self.assertEqual(transition["stage_shift"]["stage_id"], str(call_requested.id))
        self.assertIn("preferred callback time", decision.message)
        self.assertIn("T17:00:00", reminder["due_at"])
        self.assertNotIn("can’t confirm a callback", decision.message)

    def test_failsoft_missed_booked_call_routes_to_human_follow_up(self):
        human_stage = self._stage("Human Intervention Needed", 20)
        inbound = self._inbound("wamid-missed-booked-call")
        inbound.body = "I have booked the call, but no one called"
        inbound.save(update_fields=["body", "updated_at"])

        decision = build_deterministic_fallback_decision(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=inbound,
        )

        transition = next(
            action
            for action in decision.crm_actions
            if action.get("type") == "pipeline_transition"
        )
        self.assertEqual(transition["stage_shift"]["stage_id"], str(human_stage.id))
        self.assertTrue(any(action.get("type") == "add_note" for action in decision.crm_actions))
        self.assertEqual(decision.reason_code, "HUMAN_HANDOFF")
        self.assertIn("flagged", decision.message)

    def test_completion_ack_is_persisted_once_in_runtime_state(self):
        decision = SimpleNamespace(
            should_engage=True,
            message="Thanks for sharing the details. Our team will connect with you shortly.",
            next_requirement_id=None,
        )
        qualification = {
            "qualification_status": "completed",
            "engagement_mode": "conversation",
            "requirement_states": {},
        }

        finalize_runtime(
            lead=self.lead,
            decision=decision,
            qualification=qualification,
            requirements=[],
            message_id="final-q5-message",
        )
        self.lead.refresh_from_db()
        runtime = (self.lead.attributes or {}).get(STATE_KEY, {})

        self.assertTrue(runtime.get("qualification_completion_ack_sent"))
        self.assertEqual(
            runtime.get("qualification_completion_ack_message_id"),
            "final-q5-message",
        )
        self.assertTrue(runtime.get("qualification_completion_ack_hash"))
