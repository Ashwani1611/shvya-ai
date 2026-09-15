from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.services.qualification_state import MODE_CONVERSATION


class ConversationCRMActionRuntimeTests(SimpleTestCase):
    """Non-qualification CRM actions remain available in normal conversation.

    Qualification attribute mapping/completion is intentionally no longer tested
    here because that authority moved to qualification_execution_contract and its
    production-equivalent test suite.
    """

    def _context(self, *, stage_name: str, latest_text: str, available_stages=None):
        return SimpleNamespace(
            stage={"id": "current-stage", "name": stage_name},
            pipeline={
                "attribute_definitions": [],
                "available_stages": available_stages or [],
            },
            conversation={
                "messages": [{
                    "id": "m1",
                    "direction": "inbound",
                    "body": latest_text,
                }],
            },
        )

    def test_explicit_call_request_creates_immediate_reminder(self):
        before = timezone.now()
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
            context=self._context(
                stage_name="Follow-up",
                latest_text="I want to connect, please call me",
            ),
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={
                "engagement_mode": MODE_CONVERSATION,
                "requirement_states": {},
            },
            requirements=[],
        )
        reminder = next(item for item in actions if item.get("type") == "create_reminder")
        due_at = timezone.datetime.fromisoformat(reminder["due_at"])
        self.assertGreaterEqual(due_at, before)
        self.assertLessEqual(due_at, timezone.now() + timezone.timedelta(minutes=1))

    def test_other_stage_stays_conversation_only_but_can_shift_by_stage_rule(self):
        destination = {
            "id": "human-stage",
            "name": "Human Intervention",
            "description": "Move here when the lead explicitly asks to talk to a person.",
            "pipeline_id": "other-pipeline",
            "pipeline_name": "Escalations",
        }
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(
                qualification_updates=[{
                    "requirement_id": "budget",
                    "value": "50k",
                    "source_message_id": "m1",
                    "evidence": "50k",
                }],
                crm_actions=[{
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": "human-stage"},
                }],
            ),
            context=self._context(
                stage_name="Follow-up",
                latest_text="I want to talk to a person",
                available_stages=[destination],
            ),
            runtime_policy={
                "qualification": {
                    "criteria": [{"id": "budget", "required": True}],
                },
            },
            qualification_state={
                "engagement_mode": MODE_CONVERSATION,
                "requirement_states": {
                    "budget": {"status": "unknown", "value": None},
                },
            },
            requirements=[{"id": "budget", "required": True}],
        )

        self.assertTrue(any(
            item.get("type") == "pipeline_transition"
            and item.get("stage_shift", {}).get("stage_id") == "human-stage"
            for item in actions
        ))
        self.assertFalse(any(
            item.get("type") == "attribute_updates"
            for item in actions
        ))
