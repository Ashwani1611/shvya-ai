"""Source quotes cannot authorize qualification values they contradict."""
from copy import deepcopy
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_state import project_answer_updates, state_for_lead
from apps.ai_engagement.tests.test_first_turn_volunteered_capture import _QUESTIONS


class QualificationEvidenceConsistencyTests(SimpleTestCase):
    def setUp(self):
        self.requirements = compile_qualification_requirements(_QUESTIONS)["requirements"]
        self.state = state_for_lead(SimpleNamespace(attributes={}, stage=SimpleNamespace(name="New leads")),
                                    requirements=self.requirements)
        self.body = (
            "My main problem is slow replies. I manage my leads using Excel. "
            "I receive 20 leads per day. Yes, I am currently running paid ads."
        )
        self.messages = [{"id": "fresh-inbound", "direction": "inbound", "body": self.body}]
        self.updates = [
            {"requirement_id": requirement["id"], "value": value,
             "source_message_id": "fresh-inbound", "evidence": quote}
            for requirement, value, quote in zip(self.requirements,
                ("Slow replies", "Excel / Google Sheets", "11–30", "Yes"),
                ("slow replies", "Excel", "20 leads per day", "Yes, I am currently running paid ads"), strict=True)
        ]

    def project(self, updates):
        return project_answer_updates(state=self.state, requirements=self.requirements,
                                      updates=updates, messages=self.messages)

    def test_live_volunteered_answers_preserve_the_stated_values(self):
        result = self.project(self.updates)
        self.assertTrue(result["qualification_completed"])
        self.assertEqual(result["qualification_answers"],
                         {item["requirement_id"]: item["value"] for item in self.updates})

    def test_allowed_last_options_are_rejected_when_the_quotes_say_otherwise(self):
        for index, contradicted in enumerate(("No proper tracking", "Multiple places", "More than 30", "No")):
            with self.subTest(value=contradicted):
                updates = deepcopy(self.updates)
                updates[index]["value"] = contradicted
                with self.assertRaisesRegex(ValueError, "contradicts.*evidence"):
                    self.project(updates)

    def test_negative_status_quote_cannot_authorize_yes(self):
        self.messages[0]["body"] = "I am not currently running paid ads."
        update = {**self.updates[3], "evidence": "I am not currently running paid ads", "value": "Yes"}
        with self.assertRaisesRegex(ValueError, "contradicts.*evidence"):
            self.project([update])
        self.assertEqual(self.project([{**update, "value": "No"}])["qualification_answers"],
                         {update["requirement_id"]: "No"})

    def test_quote_cannot_drop_negation_from_its_source_sentence(self):
        self.messages[0]["body"] = "I am not currently running paid ads."
        update = {**self.updates[3], "evidence": "currently running paid ads", "value": "Yes"}
        with self.assertRaisesRegex(ValueError, "contradicts.*evidence"):
            self.project([update])

    def test_unrecognized_negative_grammar_is_not_mistaken_for_affirmation(self):
        for body in (
            "We currently have no paid ads.",
            "We currently aren't running paid ads.",
            "We currently don’t run paid ads.",
            "We currently have no trouble running paid ads.",
            "No worries, we are currently running paid ads.",
            "Yes to CRM, no to paid ads.",
            "No, I use Excel and run paid ads.",
        ):
            with self.subTest(body=body):
                self.messages[0]["body"] = body
                # Defer unfamiliar forms to the semantic validator rather than
                # deciding from the presence of currently plus a verb.
                for selected in ("Yes", "No"):
                    update = {**self.updates[3], "evidence": body, "value": selected}
                    self.assertEqual(self.project([update])["qualification_answers"],
                                     {update["requirement_id"]: selected})

    def test_unrelated_no_prefix_does_not_override_the_quoted_ads_statement(self):
        self.messages[0]["body"] = "No problem with payments, we currently run paid ads."
        update = {**self.updates[3], "evidence": "we currently run paid ads", "value": "Yes"}
        self.assertEqual(self.project([update])["qualification_answers"],
                         {update["requirement_id"]: "Yes"})

    def test_daily_band_boundaries_follow_the_authored_question(self):
        for number, expected in ((10, "0–10"), (11, "11–30"), (30, "11–30"), (31, "More than 30")):
            with self.subTest(number=number):
                body = f"I receive {number} leads per day."
                self.messages[0]["body"] = body
                update = {**self.updates[2], "evidence": body, "value": expected}
                self.assertEqual(self.project([update])["qualification_answers"],
                                 {update["requirement_id"]: expected})
                wrong = "More than 30" if expected != "More than 30" else "11–30"
                with self.assertRaisesRegex(ValueError, "contradicts.*evidence"):
                    self.project([{**update, "value": wrong}])

    def test_explicit_correction_and_multiple_options_keep_semantic_validation(self):
        for body, expected in (
            ("I use Excel and CRM.", "Multiple places"),
            ("Actually, I use CRM, not Excel.", "CRM"),
        ):
            with self.subTest(body=body):
                self.messages[0]["body"] = body
                update = {**self.updates[1], "evidence": body, "value": expected}
                self.assertEqual(self.project([update])["qualification_answers"],
                                 {update["requirement_id"]: expected})

    def test_short_quote_retains_other_tools_stated_in_its_source_clause(self):
        self.messages[0]["body"] = "I use Excel and CRM."
        update = {**self.updates[1], "evidence": "Excel", "value": "Multiple places"}
        self.assertEqual(self.project([update])["qualification_answers"],
                         {update["requirement_id"]: "Multiple places"})

    def test_short_numeric_quote_keeps_compound_volume_ambiguous(self):
        self.messages[0]["body"] = "I receive 20 warm leads and 30 cold leads per day."
        update = {**self.updates[2], "evidence": "20 warm leads", "value": "More than 30"}
        self.assertEqual(self.project([update])["qualification_answers"],
                         {update["requirement_id"]: "More than 30"})

    def test_multi_tool_alias_keeps_the_transactional_final_answer(self):
        requirements = compile_qualification_requirements(
            "[id: lead_system] Where do you currently manage leads?\n"
            "A. WhatsApp chats\nB. Excel / Sheets\nC. CRM\nD. Multiple places"
        )["requirements"]
        state = state_for_lead(SimpleNamespace(attributes={}, stage=SimpleNamespace(name="New leads")),
                               requirements=requirements)
        source = "I use WhatsApp and Excel, and call me tomorrow at 5 PM."
        update = {"requirement_id": requirements[0]["id"], "value": "Multiple places",
                  "source_message_id": "final-inbound", "evidence": "WhatsApp and Excel"}
        result = project_answer_updates(state=state, requirements=requirements, updates=[update],
            messages=[{"id": "final-inbound", "direction": "inbound", "body": source}])
        self.assertTrue(result["qualification_completed"])
        self.assertEqual(result["qualification_answers"], {requirements[0]["id"]: "Multiple places"})

    def test_unrelated_callback_does_not_make_one_tool_multiple_tools(self):
        self.messages[0]["body"] = "I use Excel, and call me tomorrow at 5 PM."
        update = {**self.updates[1], "evidence": "Excel", "value": "Multiple places"}
        with self.assertRaisesRegex(ValueError, "contradicts.*evidence"):
            self.project([update])

    def test_ambiguous_or_translated_evidence_keeps_semantic_validation(self):
        self.messages[0]["body"] = "Meine größte Schwierigkeit sind langsame Antworten."
        update = {**self.updates[0], "evidence": "langsame Antworten"}
        self.assertEqual(self.project([update])["qualification_answers"],
                         {update["requirement_id"]: "Slow replies"})
