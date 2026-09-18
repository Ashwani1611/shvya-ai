from django.test import SimpleTestCase

from apps.ai_engagement.graph.evidence import _safe_qualification_grounding_decision
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.qualification_scoring import calculate_lead_score
from apps.ai_engagement.services.qualification_state import (
    _match_option_answer,
    requirements_for_lead,
)


class _FakeMessages:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, **kwargs):
        return self

    def order_by(self, *args):
        return self

    def values(self, *fields):
        return [{field: row.get(field) for field in fields} for row in self.rows]


class _FakeLead:
    def __init__(self, *, rows=None, attributes=None):
        self.whatsapp_messages = _FakeMessages(rows or [])
        self.attributes = attributes or {}


class QualificationReliabilityRegressionTests(SimpleTestCase):
    def test_singular_reply_matches_plural_authored_option(self):
        options = [
            {"key": "A", "value": "Slow replies"},
            {"key": "B", "value": "Missed follow-ups"},
        ]
        self.assertEqual(_match_option_answer("Slow reply", options), "Slow replies")

    def test_conservative_inflection_does_not_create_fuzzy_match(self):
        options = [
            {"key": "A", "value": "Slow replies"},
            {"key": "B", "value": "Missed follow-ups"},
        ]
        self.assertIsNone(_match_option_answer("need automation", options))

    def test_in_progress_lead_can_adopt_exact_prefix_flow_shrink(self):
        old = [
            {"id": f"q{i}", "stable_id": f"qualification_{i}", "question": f"Q{i}"}
            for i in range(1, 8)
        ]
        current = old[:5]
        lead = _FakeLead(
            attributes={
                "_shvya_ai_qualification": {
                    "qualification_status": "in_progress",
                    "flow_snapshot": old,
                }
            }
        )
        effective = requirements_for_lead(lead, current)
        self.assertEqual([item["stable_id"] for item in effective], [
            f"qualification_{i}" for i in range(1, 6)
        ])

    def test_unrelated_flow_edit_keeps_pinned_snapshot(self):
        old = [
            {"id": "q1", "stable_id": "qualification_1", "question": "Old Q1"},
            {"id": "q2", "stable_id": "qualification_2", "question": "Old Q2"},
        ]
        current = [
            {"id": "new", "stable_id": "different_1", "question": "Different"},
        ]
        lead = _FakeLead(
            attributes={
                "_shvya_ai_qualification": {
                    "qualification_status": "in_progress",
                    "flow_snapshot": old,
                }
            }
        )
        self.assertEqual(requirements_for_lead(lead, current), old)


class QualificationScoreTests(SimpleTestCase):
    def test_eighty_percent_answered_forces_score_to_eight(self):
        requirements = [
            {"id": f"q{i}", "required": True}
            for i in range(1, 6)
        ]
        state = {
            "requirement_states": {
                "q1": {"status": "answered"},
                "q2": {"status": "answered"},
                "q3": {"status": "answered"},
                "q4": {"status": "answered"},
                "q5": {"status": "unknown"},
            }
        }
        lead = _FakeLead(rows=[
            {"id": "1", "body": "Slow reply"},
            {"id": "2", "body": "WhatsApp chats"},
            {"id": "3", "body": "10-30"},
            {"id": "4", "body": "Yes"},
        ])
        score = calculate_lead_score(
            lead=lead,
            qualification_state=state,
            requirements=requirements,
        )
        self.assertTrue(score["eighty_percent_override"])
        self.assertGreaterEqual(score["score"], 8)
        self.assertTrue(score["meets_qualification_threshold"])

    def test_score_components_are_bounded_to_ten(self):
        requirements = [{"id": "q1", "required": True}]
        state = {"requirement_states": {"q1": {"status": "answered"}}}
        lead = _FakeLead(rows=[
            {
                "id": "1",
                "body": (
                    "We need this ASAP this week. Our sales workflow has lead leakage, "
                    "missed follow-ups and conversion-rate issues. I am the decision-maker, "
                    "budget is approved, and I want to schedule a demo."
                ),
            }
        ])
        score = calculate_lead_score(
            lead=lead,
            qualification_state=state,
            requirements=requirements,
        )
        self.assertEqual(score["score"], 10)
        self.assertEqual(score["components"]["engagement"]["max"], 3)
        self.assertEqual(score["components"]["urgency_timeline"]["max"], 3)
        self.assertEqual(score["components"]["clarity_of_need"]["max"], 2)
        self.assertEqual(score["components"]["commitment_signal"]["max"], 2)


class QualificationGroundingRegressionTests(SimpleTestCase):
    def test_grounding_rejection_uses_neutral_ack_for_qualification_turn(self):
        decision = EngagementDecision(
            should_engage=True,
            message="Unsupported claim",
            file_document_id=None,
            crm_actions=[],
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            next_requirement_id="q2",
            model="test",
        )
        safe = _safe_qualification_grounding_decision(
            {
                "latest_message_id": "m1",
                "qualification_state": {
                    "requirement_states": {
                        "q1": {
                            "status": "answered",
                            "source_message_id": "m1",
                        }
                    }
                },
            },
            decision,
        )
        self.assertIsNotNone(safe)
        self.assertEqual(safe.message, "Thanks for sharing that.")
        self.assertEqual(safe.next_requirement_id, "q2")
        self.assertNotIn("verified information", safe.message.casefold())
