from types import SimpleNamespace

from django.test import TestCase

from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.services.engagement_failsoft import (
    build_deterministic_fallback_decision,
)
from apps.ai_engagement.services.qualification_state import (
    MODE_CONVERSATION,
    MODE_QUALIFICATION,
    STATUS_COMPLETED,
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

    def test_in_conversation_is_normal_conversation_only(self):
        in_conversation = self._stage("In Conversation", 20)
        self.lead.stage = in_conversation
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()

        state = state_for_lead(self.lead)

        self.assertNotEqual(state["qualification_status"], STATUS_COMPLETED)
        self.assertEqual(state["engagement_mode"], MODE_CONVERSATION)

    def test_mark_in_progress_does_not_reopen_qualification_outside_new_lead(self):
        in_conversation = self._stage("In Conversation", 20)
        self.lead.stage = in_conversation
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()

        before = state_for_lead(self.lead)
        state = mark_in_progress(self.lead)

        # The lifecycle may remain in_progress so existing answers are preserved,
        # but qualification must stay paused outside New Lead.
        self.assertEqual(state["qualification_status"], before["qualification_status"])
        self.assertEqual(state["engagement_mode"], MODE_CONVERSATION)

    def test_first_reply_does_not_auto_move_qualifying_lead_to_in_conversation(self):
        in_conversation = self._stage("In Conversation", 20)
        context = SimpleNamespace(
            conversation={
                "messages": [
                    {"id": "m1", "direction": "inbound", "body": "Yes"},
                ]
            },
            pipeline={
                "attribute_definitions": [],
                "available_stages": [
                    {
                        "id": str(in_conversation.id),
                        "name": "In Conversation",
                        "is_current_pipeline": True,
                        "pipeline_name": self.pipeline.name,
                    },
                    {
                        "id": str(self.qualified.id),
                        "name": "Qualified",
                        "is_current_pipeline": True,
                        "pipeline_name": self.pipeline.name,
                    },
                ],
            },
            stage={"id": str(self.new_lead.id), "name": "New leads"},
        )
        state = {
            "qualification_status": "in_progress",
            "engagement_mode": MODE_QUALIFICATION,
            "requirement_states": {},
            "qualified_stage_id": str(self.qualified.id),
        }
        decision = SimpleNamespace(
            qualification_updates=[],
            crm_actions=[],
            reason_code="QUALIFICATION_NEXT",
            should_engage=True,
            message="",
            next_requirement_id=None,
        )

        actions, result = build_controlled_actions(
            decision=decision,
            context=context,
            runtime_policy={"qualification": {"criteria": []}, "crm": {}},
            qualification_state=state,
            requirements=[],
        )

        self.assertFalse(
            any(
                action.get("type") == "pipeline_transition"
                and (action.get("stage_shift") or {}).get("stage_id")
                == str(in_conversation.id)
                for action in actions
            )
        )
        self.assertNotEqual(
            (result.get("stage_transition") or {}).get("source"),
            "first_genuine_inbound",
        )

    def test_completed_new_lead_moves_directly_to_qualified_not_in_conversation(self):
        in_conversation = self._stage("In Conversation", 20)
        requirement = {
            "id": "budget",
            "stable_id": "budget",
            "label": "Budget",
            "question": "What is your budget?",
            "required": True,
            "priority": 1,
        }
        context = SimpleNamespace(
            conversation={
                "messages": [
                    {"id": "m-final", "direction": "inbound", "body": "50000"},
                ]
            },
            pipeline={
                "attribute_definitions": [],
                "available_stages": [
                    {
                        "id": str(in_conversation.id),
                        "name": "In Conversation",
                        "is_current_pipeline": True,
                        "pipeline_name": self.pipeline.name,
                    },
                    {
                        "id": str(self.qualified.id),
                        "name": "Qualified",
                        "is_current_pipeline": True,
                        "pipeline_name": self.pipeline.name,
                    },
                ],
            },
            stage={"id": str(self.new_lead.id), "name": "New leads"},
        )
        state = {
            "qualification_status": "completed",
            "qualification_completed": True,
            "all_requirements_answered": True,
            "engagement_mode": MODE_CONVERSATION,
            "requirement_states": {
                "budget": {
                    "status": "answered",
                    "value": "50000",
                    "source_message_id": "m-final",
                    "raw_answer": "50000",
                }
            },
            "qualified_stage_id": str(self.qualified.id),
        }
        decision = SimpleNamespace(
            qualification_updates=[],
            crm_actions=[],
            reason_code="QUALIFICATION_COMPLETE",
            should_engage=True,
            message="Thanks.",
            next_requirement_id=None,
        )

        actions, _result = build_controlled_actions(
            decision=decision,
            context=context,
            runtime_policy={
                "qualification": {
                    "criteria": [
                        {
                            "id": "budget",
                            "required": True,
                            "pass_condition": None,
                        }
                    ]
                },
                "crm": {},
            },
            qualification_state=state,
            requirements=[requirement],
        )

        transitions = [
            action
            for action in actions
            if action.get("type") == "pipeline_transition"
        ]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(
            transitions[0]["stage_shift"]["stage_id"],
            str(self.qualified.id),
        )
        self.assertNotEqual(
            transitions[0]["stage_shift"]["stage_id"],
            str(in_conversation.id),
        )

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
