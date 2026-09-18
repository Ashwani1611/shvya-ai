from django.test import TestCase

from apps.ai_engagement.services.intent_score import (
    INTENT_SCORE_STATE_KEY,
    compute_intent_score,
    persist_intent_score,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class IntentScoreTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Intent Score Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            is_active=True,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New Lead",
            is_active=True,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            phone_number_id="intent-score-account",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Intent Lead",
            phone="+919999000111",
            attributes={},
        )

    def _inbound(self, body):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            body=body,
            from_number=self.lead.phone,
            to_number="+919000000000",
        )

    def test_score_uses_exact_0_to_10_rubric_without_provider_call(self):
        self._inbound("We need to automate lead follow-ups in our CRM.")
        self._inbound("We need this within 7 days.")
        self._inbound("Please schedule a demo. Our budget is 28000.")
        self._inbound("We handle around 180 customer conversations every month.")

        result = compute_intent_score(lead=self.lead)

        self.assertEqual(result["score"], 10)
        self.assertEqual(result["components"]["engagement"]["score"], 3)
        self.assertEqual(result["components"]["urgency"]["score"], 3)
        self.assertEqual(result["components"]["clarity"]["score"], 2)
        self.assertEqual(result["components"]["commitment"]["score"], 2)

    def test_score_uses_normalized_option_answers_for_timeline_and_budget(self):
        self.lead.attributes = {
            "_shvya_ai_qualification": {
                "qualification_status": "in_progress",
                "answered_requirement_ids": ["timeline", "budget"],
                "flow_snapshot": [
                    {
                        "id": "timeline",
                        "label": "How soon are you planning to implement a solution?",
                    },
                    {
                        "id": "budget",
                        "label": "What is your approximate monthly budget?",
                    },
                ],
                "requirement_states": {
                    "timeline": {
                        "status": "answered",
                        "value": "Within 7 days",
                    },
                    "budget": {
                        "status": "answered",
                        "value": "₹25,000–₹50,000",
                    },
                },
            }
        }
        self.lead.save(update_fields=["attributes", "updated_at"])
        self._inbound("B")
        self._inbound("28000")

        result = compute_intent_score(lead=self.lead)

        self.assertEqual(result["components"]["urgency"]["score"], 3)
        self.assertEqual(result["components"]["commitment"]["score"], 1)

    def test_answering_eighty_percent_of_required_questions_forces_minimum_eight(self):
        self.lead.attributes = {
            "_shvya_ai_qualification": {
                "qualification_status": "in_progress",
                "flow_snapshot": [
                    {"id": f"q{i}", "required": True, "label": f"Question {i}"}
                    for i in range(1, 6)
                ],
                "requirement_states": {
                    "q1": {"status": "answered", "value": "A"},
                    "q2": {"status": "answered", "value": "B"},
                    "q3": {"status": "answered", "value": "C"},
                    "q4": {"status": "answered", "value": "D"},
                    "q5": {"status": "unknown"},
                },
            }
        }
        self.lead.save(update_fields=["attributes", "updated_at"])
        self._inbound("A")
        self._inbound("B")
        self._inbound("C")
        self._inbound("D")

        result = compute_intent_score(lead=self.lead)

        self.assertTrue(result["eighty_percent_override"])
        self.assertEqual(result["questions_answered"], 4)
        self.assertEqual(result["questions_required"], 5)
        self.assertEqual(result["answered_ratio"], 0.8)
        self.assertGreaterEqual(result["score"], 8)
        self.assertTrue(result["meets_qualified_threshold"])

    def test_just_exploring_has_zero_urgency(self):
        self._inbound("I am just exploring options right now.")

        result = compute_intent_score(lead=self.lead)

        self.assertEqual(result["components"]["urgency"]["score"], 0)

    def test_decision_maker_signal_counts_as_strong_commitment(self):
        self._inbound("I am the final decision-maker for this purchase.")

        result = compute_intent_score(lead=self.lead)

        self.assertEqual(result["components"]["commitment"]["score"], 2)

    def test_score_persists_as_internal_lead_intelligence(self):
        self._inbound("I am interested in pricing.")
        result = persist_intent_score(lead=self.lead)
        self.lead.refresh_from_db()

        self.assertIn(INTENT_SCORE_STATE_KEY, self.lead.attributes)
        self.assertEqual(
            self.lead.attributes[INTENT_SCORE_STATE_KEY]["score"],
            result["score"],
        )
        self.assertNotIn("intent_score", {
            key: value
            for key, value in self.lead.attributes.items()
            if key != INTENT_SCORE_STATE_KEY
        })
