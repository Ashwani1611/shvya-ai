from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.qualification_execution.actions import (
    answers_captured_for_source,
    rebuild_mapped_attribute_actions,
)
from apps.ai_engagement.services.qualification_execution.completion import _configured_completion_reminders


class QualificationActionBuildingTests(SimpleTestCase):
    @patch("django.utils.timezone.now", return_value=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
    def test_authored_completion_reminder_uses_organization_calendar_day(self, now):
        actions = _configured_completion_reminders({
            "timezone": "America/New_York",
            "reminder_rules": [
                "After all qualification questions are answered, create a reminder tomorrow at 3 PM.",
            ],
        })
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["due_at"], "2026-10-05T15:00:00-04:00")

    def test_exact_mapping_overrides_model_destination_and_preserves_distinct_fact(self):
        actions = [{"type": "attribute_updates", "updates": [
            {"key": "tool", "value": "Excel"},
            {"key": "shadow", "value": "WhatsApp"},
            {"key": "team_size", "value": 12},
        ]}, {"type": "contact_updates", "updates": []}]
        original = deepcopy(actions)
        rebuilt = rebuild_mapped_attribute_actions(
            actions=actions,
            updates=[{"requirement_id": "lead_tool", "value": "WhatsApp"}],
            requirements=[{"id": "lead_tool", "stable_id": "lead_tool", "question": "Where do you manage leads?"}],
            config={"mapping_targets": {"lead_tool": ["tool"]}},
        )
        self.assertEqual(rebuilt[0]["updates"], [
            {"key": "team_size", "value": 12}, {"key": "tool", "value": "WhatsApp"},
        ])
        self.assertEqual(rebuilt[1], actions[1])
        self.assertEqual(actions, original)

    def test_boolean_false_remains_a_supported_qualification_value(self):
        rebuilt = rebuild_mapped_attribute_actions(
            actions=[{"type": "attribute_updates", "updates": [{"key": "shadow", "value": False}]}],
            updates=[{"requirement_id": "ads", "value": False}],
            requirements=[{"id": "ads", "question": "Do you run ads?"}], config={"mappings": {"ads": "running_ads"}},
        )
        self.assertEqual(rebuilt, [{"type": "attribute_updates", "updates": [
            {"key": "running_ads", "value": False},
        ]}])

    def test_graph_answers_require_this_sources_exact_nonempty_inbound_evidence(self):
        source = SimpleNamespace(pk="current", body="We use WhatsApp and receive 20 leads.")
        state = {"requirement_states": {
            "tool": {"status": "answered", "value": "WhatsApp", "raw_answer": "We use WhatsApp",
                     "source_message_id": "current"},
            "stale": {"status": "answered", "value": 20, "raw_answer": "20 leads",
                      "source_message_id": "old"},
            "invented": {"status": "answered", "value": "Excel", "raw_answer": "We use Excel",
                         "source_message_id": "current"},
            "blank": {"status": "answered", "value": "Yes", "raw_answer": "",
                      "source_message_id": "current"},
        }}
        self.assertEqual(answers_captured_for_source(state=state, source=source), [{
            "requirement_id": "tool", "value": "WhatsApp", "source_message_id": "current",
            "evidence": "We use WhatsApp",
        }])
