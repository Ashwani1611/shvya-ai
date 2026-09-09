from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.graph.policy_actions import (
    build_controlled_actions,
    evaluate_condition,
    evaluate_qualification,
)
from apps.ai_engagement.graph.runtime_policy import compile_runtime_policy
from apps.ai_engagement.graph.workflow import ENGAGEMENT_GRAPH


class RuntimePolicyTests(SimpleTestCase):
    def test_compiles_authored_rules_and_numeric_qualification_threshold(self):
        organization = SimpleNamespace(id="org-1")
        profile = {
            "identity": {"name": "Acme", "about": "Premium solar installer"},
            "communication": {
                "languages": ["English", "Hindi"],
                "custom_instructions": "Be concise.\nNever pressure the lead.",
            },
            "qualification": {
                "mode": "all_required",
                "raw": "Budget should be at least 50k?",
                "requirements": [
                    {
                        "id": "budget_should_be_at_least_50k",
                        "label": "Budget should be at least 50k",
                        "question": "Budget should be at least 50k?",
                        "required": True,
                        "priority": 1,
                        "can_direct_ask": True,
                    }
                ],
            },
        }

        policy = compile_runtime_policy(organization=organization, profile=profile)

        self.assertEqual(policy["engagement"]["rules"], ["Be concise.", "Never pressure the lead."])
        criterion = policy["qualification"]["criteria"][0]
        self.assertEqual(criterion["pass_condition"], {"operator": "gte", "value": 50000.0})
        self.assertEqual(policy["organization"]["languages"], ["English", "Hindi"])

    def test_numeric_and_boolean_conditions_are_deterministic(self):
        self.assertEqual(evaluate_condition("75k", {"operator": "gte", "value": 50000}), "pass")
        self.assertEqual(evaluate_condition("40k", {"operator": "gte", "value": 50000}), "fail")
        self.assertEqual(evaluate_condition("yes", {"operator": "truthy", "value": True}), "pass")
        self.assertEqual(evaluate_condition("maybe", {"operator": "truthy", "value": True}), "unknown")

    def test_graph_is_compiled(self):
        self.assertTrue(hasattr(ENGAGEMENT_GRAPH, "invoke"))


class ControlledCRMActionTests(SimpleTestCase):
    def _runtime_policy(self):
        return {
            "qualification": {
                "criteria": [
                    {
                        "id": "budget",
                        "label": "Budget",
                        "required": True,
                        "priority": 1,
                        "pass_condition": {"operator": "gte", "value": 50000},
                    }
                ]
            }
        }

    def _qualification_state(self):
        return {
            "qualification_status": "in_progress",
            "requirement_states": {
                "budget": {
                    "status": "unknown",
                    "value": None,
                    "confidence": "",
                    "source_message_id": None,
                    "updated_at": None,
                }
            },
            "qualified_stage_id": "qualified-stage-id",
        }

    def _context(self, body="My budget is 75k"):
        return SimpleNamespace(
            conversation={
                "messages": [
                    {
                        "id": "message-1",
                        "direction": "inbound",
                        "body": body,
                    }
                ]
            },
            pipeline={
                "attribute_definitions": [
                    {"key": "budget", "name": "Budget", "field_type": "text"}
                ]
            },
        )

    def test_model_cannot_choose_stage_and_python_adds_qualified_stage(self):
        decision = SimpleNamespace(
            qualification_updates=[
                {
                    "requirement_id": "budget",
                    "value": "75k",
                    "source_message_id": "message-1",
                    "evidence": "75k",
                }
            ],
            crm_actions=[
                {
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": "model-chosen-stage"},
                }
            ],
        )

        actions, result = build_controlled_actions(
            decision=decision,
            context=self._context(),
            runtime_policy=self._runtime_policy(),
            qualification_state=self._qualification_state(),
            requirements=[{"id": "budget", "required": True}],
        )

        self.assertEqual(result["evaluation"]["outcome"], "qualified")
        self.assertNotIn("model-chosen-stage", str(actions))
        self.assertIn(
            {
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": "qualified-stage-id"},
            },
            actions,
        )

    def test_failed_required_condition_does_not_advance_stage(self):
        decision = SimpleNamespace(
            qualification_updates=[
                {
                    "requirement_id": "budget",
                    "value": "40k",
                    "source_message_id": "message-1",
                    "evidence": "40k",
                }
            ],
            crm_actions=[],
        )

        actions, result = build_controlled_actions(
            decision=decision,
            context=self._context(body="My budget is 40k"),
            runtime_policy=self._runtime_policy(),
            qualification_state=self._qualification_state(),
            requirements=[{"id": "budget", "required": True}],
        )

        self.assertEqual(result["evaluation"]["outcome"], "not_qualified")
        self.assertFalse(any(action["type"] == "pipeline_transition" for action in actions))

    def test_evidence_backed_qualification_value_updates_matching_attribute(self):
        decision = SimpleNamespace(
            qualification_updates=[
                {
                    "requirement_id": "budget",
                    "value": "75k",
                    "source_message_id": "message-1",
                    "evidence": "75k",
                }
            ],
            crm_actions=[],
        )

        actions, _ = build_controlled_actions(
            decision=decision,
            context=self._context(),
            runtime_policy=self._runtime_policy(),
            qualification_state=self._qualification_state(),
            requirements=[{"id": "budget", "required": True}],
        )

        attribute_action = next(action for action in actions if action["type"] == "attribute_updates")
        self.assertEqual(attribute_action["updates"], [{"key": "budget", "value": "75k"}])

    def test_unrequested_model_reminder_is_removed(self):
        decision = SimpleNamespace(
            qualification_updates=[],
            crm_actions=[
                {
                    "type": "create_reminder",
                    "title": "Call lead",
                    "description": "AI invented this",
                    "due_at": "2026-09-11T10:00:00+05:30",
                }
            ],
        )

        actions, _ = build_controlled_actions(
            decision=decision,
            context=self._context(body="Please send me the brochure"),
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={"requirement_states": {}},
            requirements=[],
        )
        self.assertEqual(actions, [])
