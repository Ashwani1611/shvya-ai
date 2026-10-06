from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.playground import _SandboxLead
from apps.ai_engagement.services.qualification_state import (
    apply_unambiguous_reply, record_last_asked_requirement,
)
from apps.ai_engagement.services.sandbox_display_state import (
    displayed_requirement_id, sandbox_customer_name,
)


class SandboxDisplayStateTests(SimpleTestCase):
    def setUp(self):
        self.requirements = [
            {"id": "name", "label": "Please provide your Full Name", "question": "Please provide your Full Name.", "priority": 1, "can_direct_ask": True},
            {"id": "age", "label": "Please provide your Age", "question": "Please provide your Age.", "priority": 2, "can_direct_ask": True},
            {"id": "date", "label": "Preferred date", "question": "Please provide your Preferred Date.", "priority": 3, "can_direct_ask": True},
        ]
        self.lead = _SandboxLead(pk="playground:test", attributes={}, name="",
                                 stage=SimpleNamespace(name="New leads"))

    def decision(self, message, requirement=None):
        return SimpleNamespace(should_engage=True, message=message, next_requirement_id=requirement)

    def test_displayed_question_without_model_id_records_age_for_next_answer(self):
        record_last_asked_requirement(self.lead, "name", requirements=self.requirements)
        apply_unambiguous_reply(lead=self.lead, requirements=self.requirements,
                               text="Alex QA", source_message_id="turn:1")
        identifier = displayed_requirement_id(
            lead=self.lead, requirements=self.requirements,
            decision=self.decision("Thank you, Alex. Please provide your Age so we can continue."),
        )
        self.assertEqual(identifier, "age")
        record_last_asked_requirement(self.lead, identifier, requirements=self.requirements)
        result = apply_unambiguous_reply(lead=self.lead, requirements=self.requirements,
                                        text="I am 28 years old", source_message_id="turn:2")
        self.assertTrue(result["changed"])
        self.assertEqual(result["state"]["next_requirement_id"], "date")
        self.assertFalse(result["state"]["qualification_completed"])
        self.assertEqual(self.lead.name, "Alex QA")
        self.assertEqual(sandbox_customer_name(self.lead.attributes), "Alex QA")

    def test_acknowledgement_negation_quote_and_other_question_do_not_mark_pending(self):
        for message in (
            "Thanks for sharing that.",
            "Do not ask: Please provide your Full Name.",
            '"Please provide your Full Name." was an example.',
            "Please provide your Age.",
        ):
            with self.subTest(message=message):
                self.assertIsNone(displayed_requirement_id(
                    lead=self.lead, requirements=self.requirements, decision=self.decision(message),
                ))

    def test_later_stage_does_not_restart_from_displayed_words(self):
        self.lead.stage.name = "Qualified"
        self.assertIsNone(displayed_requirement_id(
            lead=self.lead, requirements=self.requirements,
            decision=self.decision("Please provide your Full Name."),
        ))

    def test_declared_id_and_no_reply_keep_existing_contract(self):
        self.assertEqual(displayed_requirement_id(
            lead=self.lead, requirements=self.requirements,
            decision=self.decision("Translated question", "name"),
        ), "name")
        decision = self.decision("Please provide your Full Name.")
        decision.should_engage = False
        self.assertIsNone(displayed_requirement_id(lead=self.lead, requirements=self.requirements, decision=decision))

    def test_company_name_does_not_become_personal_name(self):
        requirements = [{"id": "company", "label": "Please provide your company name",
                         "question": "Please provide your company name.", "priority": 1}]
        record_last_asked_requirement(self.lead, "company", requirements=requirements)
        apply_unambiguous_reply(lead=self.lead, requirements=requirements,
                               text="Example Ltd", source_message_id="turn:1")
        self.assertEqual(sandbox_customer_name(self.lead.attributes), "")
        self.assertEqual(self.lead.name, "")

    def test_exact_authored_question_preserves_non_latin_characters(self):
        requirements = [{"id": "goal", "label": "goal", "question": "आपका लक्ष्य क्या है?",
                         "priority": 1, "can_direct_ask": True}]
        self.assertEqual(displayed_requirement_id(
            lead=self.lead, requirements=requirements,
            decision=self.decision("आपका लक्ष्य क्या है?"),
        ), "goal")
