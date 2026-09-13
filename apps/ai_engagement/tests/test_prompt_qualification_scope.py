import json

from django.test import SimpleTestCase

from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.engagement import EngagementService
from apps.ai_engagement.services.langgraph_orchestration import _RUNTIME_POLICY_INSTRUCTIONS


class PromptQualificationScopeTests(SimpleTestCase):
    def _payload(self, *, engagement_mode):
        requirement = {
            "id": "biggest_challenge",
            "stable_id": "biggest_challenge",
            "question": (
                "What is your biggest challenge? "
                "A) Slow replies B) Missed follow-ups C) Leads going cold D) No proper tracking?"
            ),
            "priority": 1,
            "required": True,
            "options": [
                {"key": "A", "value": "Slow replies"},
                {"key": "B", "value": "Missed follow-ups"},
                {"key": "C", "value": "Leads going cold"},
                {"key": "D", "value": "No proper tracking"},
            ],
        }
        qstate = {
            "qualification_status": "not_started",
            "engagement_mode": engagement_mode,
            "flow_version": "flow-v1",
            "flow_snapshot": [requirement],
            "current_requirement_id": requirement["id"],
            "next_requirement_id": requirement["id"],
            "last_asked_requirement_id": None,
            "requirement_states": {
                requirement["id"]: {"status": "unknown"},
            },
            "answered_requirement_ids": [],
            "qualification_answers": {},
            "processed_message_ids": [],
        }
        profile = {
            "identity": {},
            "communication": {},
            "qualification": {"requirements": [requirement]},
        }
        context = AIContext(
            organization={
                "id": "org-1",
                "name": "SHVYA",
                "about": "SHVYA helps businesses manage existing leads.",
                "bot_languages": "English",
                "qualification_requirements": requirement["question"],
                "engagement_instructions": "Ask one question at a time.",
            },
            lead={
                "id": "lead-1",
                "attributes": {},
                "qualification": qstate,
            },
            pipeline={},
            stage={},
            contacts=[],
            attributes=[],
            conversation={
                "messages": [
                    {"id": "msg-1", "direction": "inbound", "body": "What is SHVYA?"},
                ]
            },
            conversation_summary=None,
            qualification_notes=[],
            knowledge=[],
        )
        raw = EngagementService()._build_input(
            context=context,
            profile=profile,
            qualification_state=qstate,
            next_item=requirement,
        )
        return json.loads(raw), requirement

    def test_conversation_mode_does_not_expose_pending_qualification_as_active(self):
        payload, _requirement = self._payload(engagement_mode="conversation")

        turn = payload["qualification_turn"]
        self.assertEqual(turn["mode"], "conversation")
        self.assertIsNone(turn["current_requirement"])
        self.assertIsNone(turn["next_requirement_if_current_answered"])
        self.assertFalse(turn["current_requirement_was_asked"])
        self.assertIsNone(payload["next_requirement"])
        self.assertIsNone(payload["backend_state"]["current_requirement_id"])

    def test_new_lead_qualification_mode_keeps_backend_current_requirement(self):
        payload, requirement = self._payload(engagement_mode="qualification")

        turn = payload["qualification_turn"]
        self.assertEqual(turn["mode"], "qualification")
        self.assertEqual(turn["current_requirement"]["id"], requirement["id"])
        self.assertEqual(payload["next_requirement"]["id"], requirement["id"])
        self.assertEqual(payload["backend_state"]["current_requirement_id"], requirement["id"])

    def test_runtime_policy_forbids_unverified_model_and_deployment_claims(self):
        self.assertIn("AI model names", _RUNTIME_POLICY_INSTRUCTIONS)
        self.assertIn("Never infer or confirm them from a lead message", _RUNTIME_POLICY_INSTRUCTIONS)
