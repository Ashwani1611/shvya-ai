from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.services.intent_score import (
    INTENT_SCORE_STATE_KEY,
    compute_intent_score,
    persist_intent_score,
    stored_intent_score,
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
        self._inbound("Please show me the demo. Our budget is approved and the demo is booked.")
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

    def test_answering_eighty_percent_of_asked_questions_forces_minimum_eight(self):
        self.lead.attributes = {
            "_shvya_ai_qualification": {
                "qualification_status": "in_progress",
                "flow_snapshot": [
                    {"id": f"q{i}", "required": True, "label": f"Question {i}"}
                    for i in range(1, 6)
                ],
                "requirement_states": {
                    "q1": {"status": "answered", "value": "A", "asked_at": "2026-09-19T10:00:00Z"},
                    "q2": {"status": "answered", "value": "B", "asked_at": "2026-09-19T10:00:00Z"},
                    "q3": {"status": "answered", "value": "C", "asked_at": "2026-09-19T10:00:00Z"},
                    "q4": {"status": "answered", "value": "D", "asked_at": "2026-09-19T10:00:00Z"},
                    "q5": {"status": "asked", "asked_at": "2026-09-19T10:00:00Z"},
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


class IntentScoreEvidenceTests(SimpleTestCase):
    def score(self, texts=(), states=None, questions=None):
        from types import SimpleNamespace
        lead = SimpleNamespace(_intent_texts=list(texts), attributes={
            "_shvya_ai_qualification": {"flow_snapshot": questions or [], "requirement_states": states or {}}
        })
        return compute_intent_score(lead=lead)

    def test_question_options_are_not_customer_intent(self):
        result = self.score(states={"q": {"status": "answered", "value": "Next quarter"}}, questions=[
            {"id": "q", "label": "Do you need it ASAP, next quarter, or is budget approved?"}])
        self.assertEqual(result["components"]["urgency"]["score"], 1)
        self.assertEqual(result["components"]["commitment"]["score"], 0)

    def test_newer_timeline_correction_replaces_old_urgency(self):
        result = self.score(["We need this ASAP", "Plans changed; just browsing for now."])
        self.assertEqual(result["components"]["urgency"]["score"], 0)

    def test_negated_signals_are_not_commitments(self):
        result = self.score(["It is not urgent or needed this week. Budget is not approved. No demo is booked."])
        self.assertEqual(result["components"]["urgency"]["score"], 0)
        self.assertEqual(result["components"]["commitment"]["score"], 0)

    def test_requested_demo_is_not_a_booked_demo(self):
        result = self.score(["Please book a demo"])
        self.assertEqual(result["components"]["engagement"]["score"], 3)
        self.assertEqual(result["components"]["commitment"]["score"], 1)

    def test_many_one_word_answers_do_not_inflate_engagement(self):
        result = self.score(["Retail", "Mumbai", "Tomorrow", "Twenty"])
        self.assertEqual(result["components"]["engagement"]["score"], 0)

    def test_floor_counts_only_asked_questions_and_keeps_true_components(self):
        questions = [{"id": f"q{i}", "required": i != 1} for i in range(1, 7)]
        states = {f"q{i}": {"status": "answered", "value": "A", "asked_at": "2026-09-19T10:00:00Z"} for i in range(1, 5)}
        states["q5"] = {"status": "asked"}
        states["q6"] = {"status": "unknown"}
        result = self.score(states=states, questions=questions)
        self.assertEqual(result["questions_asked"], 5)
        self.assertEqual(result["questions_answered"], 4)
        self.assertEqual(result["score"], 8)
        self.assertEqual(result["raw_score"] + result["floor_adjustment"], result["score"])
        self.assertEqual(sum(c["score"] for c in result["components"].values()), result["raw_score"])

    def test_skipped_and_empty_answers_do_not_count_as_answered(self):
        result = self.score(states={
            "q1": {"status": "skipped", "asked_at": "2026-09-19T10:00:00Z"},
            "q2": {"status": "answered", "value": "", "asked_at": "2026-09-19T10:00:00Z"},
        }, questions=[{"id": "q1"}, {"id": "q2"}])
        self.assertEqual(result["questions_answered"], 0)
        self.assertFalse(result["eighty_percent_override"])

    def test_answers_captured_before_any_question_do_not_trigger_floor(self):
        for confidence in ("verified_crm", "supported"):
            with self.subTest(confidence=confidence):
                result = self.score(states={
                    "q1": {"status": "answered", "value": "Mumbai", "asked_at": None, "confidence": confidence},
                }, questions=[{"id": "q1", "label": "Which city?"}])
                self.assertTrue(result["assessed"])
                self.assertEqual(result["questions_asked"], 0)
                self.assertEqual(result["questions_answered"], 0)
                self.assertEqual(result["answered_ratio"], 0)
                self.assertFalse(result["eighty_percent_override"])
                self.assertEqual(result["floor_adjustment"], 0)
                self.assertLess(result["score"], 8)

    def test_unasked_answers_cannot_satisfy_eighty_percent_of_asked_questions(self):
        questions = [{"id": f"q{i}", "label": f"Question {i}"} for i in range(1, 6)]
        states = {f"q{i}": {"status": "answered", "value": "A", "asked_at": None} for i in range(2, 6)}
        states["q1"] = {"status": "asked", "asked_at": "2026-09-19T10:00:00Z"}

        result = self.score(states=states, questions=questions)

        self.assertEqual(result["questions_asked"], 1)
        self.assertEqual(result["questions_answered"], 0)
        self.assertEqual(result["answered_ratio"], 0)
        self.assertFalse(result["eighty_percent_override"])
        self.assertEqual(result["floor_adjustment"], 0)
        self.assertLess(result["score"], 8)

    def test_scores_using_previous_asked_question_rule_are_not_reused(self):
        from types import SimpleNamespace

        lead = SimpleNamespace(attributes={INTENT_SCORE_STATE_KEY: {"version": 3, "score": 8}})

        self.assertIsNone(stored_intent_score(lead=lead))
        self.assertEqual(self.score()["version"], 4)

    def test_no_evidence_has_no_invented_score_or_usage(self):
        result = self.score()
        self.assertIsNone(result["score"])
        self.assertFalse(result["assessed"])
        self.assertFalse(result["eighty_percent_override"])
        self.assertEqual(result["billing"], "included_in_engagement")
