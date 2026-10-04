"""Credential-free contracts for projections, not live-model accuracy tests."""
from dataclasses import dataclass, field
from types import SimpleNamespace
import unittest

from apps.ai_engagement.services.playground_finalization import (
    enforce_preview_action_honesty,
    language_only_decision,
    needs_final_composition,
    preserve_preview_state,
    preview_file_id,
    resolved_preview_actions,
)


@dataclass(frozen=True)
class Decision:
    should_engage: bool = True
    message: str = "A supported answer."
    crm_actions: list = field(default_factory=list)
    qualification_updates: list = field(default_factory=list)
    file_document_id: int | None = None
    next_requirement_id: str | None = None


class PlaygroundFinalizationContractTests(unittest.TestCase):
    def setUp(self):
        self.visitor = SimpleNamespace(
            stage=SimpleNamespace(name="Demo Requested"), stage_id="stage-demo",
            attributes={"industry": "Retail"}, preview_reminder={"title": "Call"},
            shared_document_ids=[5],
        )

    def resolved(self, decision=None, events=None, files=None, source="turn-2"):
        return resolved_preview_actions(
            visitor=self.visitor, decision=decision or Decision(),
            events=events or [], files=files or [], source_message_id=source,
        )

    def test_plain_reply_does_not_start_another_pass(self):
        self.assertFalse(needs_final_composition(decision=Decision(), events=[], files=[]))

    def test_answer_progress_keeps_validated_next_question_without_reinterpreting_letter(self):
        decision = Decision(
            message="Where do you manage leads?\nA. WhatsApp\nB. CRM",
            qualification_updates=[{"requirement_id": "q1", "value": "Slow replies"}],
            next_requirement_id="q2",
            crm_actions=[{"type": "attribute_updates"}],
        )
        self.assertFalse(needs_final_composition(
            decision=decision, events=[{"type": "attribute_updates"}], files=[],
        ))
        for event in ({"type": "stage_transition"}, {"type": "reminder"}):
            with self.subTest(event=event):
                self.assertTrue(needs_final_composition(decision=decision, events=[event], files=[]))
        self.assertTrue(needs_final_composition(decision=decision, events=[], files=[{"id": 5}]))

    def test_silence_never_starts_final_composition(self):
        self.assertFalse(needs_final_composition(
            decision=Decision(should_engage=False, file_document_id=5), events=[], files=[{"id": 5}],
        ))

    def test_each_preview_effect_requests_final_composition(self):
        for kind in ("attribute_updates", "reminder", "stage_transition"):
            with self.subTest(kind=kind):
                self.assertTrue(needs_final_composition(
                    decision=Decision(), events=[{"type": kind, "status": "preview"}], files=[],
                ))

    def test_file_without_a_stage_change_requests_composition(self):
        self.assertTrue(needs_final_composition(decision=Decision(), events=[], files=[{"id": 5}]))

    def test_unapplied_action_attempt_also_requests_composition(self):
        self.assertTrue(needs_final_composition(
            decision=Decision(crm_actions=[{"type": "create_reminder"}]), events=[], files=[],
        ))

    def test_unavailable_file_attempt_is_not_ignored(self):
        self.assertTrue(needs_final_composition(decision=Decision(file_document_id=5), events=[], files=[]))

    def test_action_types_are_derived_from_applied_preview_events(self):
        snapshot = self.resolved(events=[{"type": "reminder", "status": "preview"}])
        self.assertEqual(snapshot["action_types"], ["create_reminder"])
        self.assertNotIn("pipeline_transition", snapshot["action_types"])

    def test_only_recognized_preview_outcomes_are_projected(self):
        snapshot = self.resolved(events=[
            {"type": "reminder", "status": "failed"},
            {"type": "stage_transition", "status": "sent"},
            {"type": "unknown", "status": "preview"}, None,
        ])
        self.assertEqual(snapshot["action_types"], [])

    def test_repeated_events_do_not_duplicate_action_types(self):
        event = {"type": "attribute_updates", "status": "preview"}
        self.assertEqual(self.resolved(events=[event, event])["action_types"], ["attribute_updates"])

    def test_proposal_without_event_is_not_claimed_completed(self):
        snapshot = self.resolved(decision=Decision(crm_actions=[{"type": "create_reminder"}]))
        self.assertEqual(snapshot["action_types"], [])
        self.assertEqual(snapshot["action_outcomes"], [{"type": "create_reminder", "status": "not_applied"}])

    def test_snapshot_identifies_this_turn_and_preview_mode(self):
        snapshot = self.resolved(source="new-turn")
        self.assertEqual(snapshot["source_message_id"], "new-turn")
        self.assertEqual(snapshot["execution_mode"], "sandbox_preview")
        self.assertEqual(snapshot["stage"]["id"], "stage-demo")

    def test_old_runtime_markers_cannot_invent_current_turn_effects(self):
        self.visitor.attributes["_shvya_ai_runtime"] = {"pre_resolved_actions": ["create_reminder"]}
        self.assertEqual(self.resolved()["action_types"], [])

    def test_file_card_is_preview_not_provider_delivery(self):
        snapshot = self.resolved(files=[{"id": 5, "name": "Guide", "url": "/private"}])
        self.assertEqual(snapshot["file_share"], {"status": "preview", "document_id": 5, "document_name": "Guide"})
        self.assertNotIn("url", snapshot["file_share"])

    def test_missing_file_does_not_reauthorize_model_selection(self):
        snapshot = self.resolved(decision=Decision(file_document_id=5))
        self.assertEqual(snapshot["file_share"], {"status": "not_previewed", "document_id": None})
        self.assertNotIn("file_share", snapshot["action_types"])

    def test_only_one_strict_integer_file_id_is_accepted(self):
        for files in (None, [], [None], [{"id": True}], [{"id": "5"}], [{"id": -1}], [{"id": 0}], [{"id": 5}, {"id": 6}]):
            with self.subTest(files=files):
                self.assertIsNone(preview_file_id(files))
        self.assertEqual(preview_file_id([{"id": 5}]), 5)

    def test_final_reply_is_language_only_and_cannot_change_file_choice(self):
        final = language_only_decision(
            decision=Decision(crm_actions=[{"type": "create_reminder"}],
                              qualification_updates=[{"value": "invented"}], file_document_id=99),
            files=[{"id": 5}],
        )
        self.assertEqual(final.crm_actions, [])
        self.assertEqual(final.qualification_updates, [])
        self.assertEqual(final.file_document_id, 5)

    def test_fallback_with_no_preview_file_cannot_create_one(self):
        final = language_only_decision(decision=Decision(file_document_id=99), files=[])
        self.assertIsNone(final.file_document_id)

    def test_final_question_and_answer_are_preserved(self):
        final = language_only_decision(decision=Decision(message="Verified answer. Next question?", next_requirement_id="q2"), files=[])
        self.assertEqual(final.next_requirement_id, "q2")
        self.assertEqual(final.message, "Verified answer. Next question?")

    def test_language_pass_restores_all_preview_state_on_success(self):
        stage = self.visitor.stage
        with preserve_preview_state(self.visitor):
            self.visitor.attributes["industry"] = "Wrong"
            self.visitor.preview_reminder["title"] = "Wrong"
            self.visitor.shared_document_ids.append(99)
            self.visitor.stage = None
            self.visitor.stage_id = "wrong"
            self.visitor.new_field = "not persisted"
        self.assertEqual(self.visitor.attributes, {"industry": "Retail"})
        self.assertEqual(self.visitor.preview_reminder, {"title": "Call"})
        self.assertEqual(self.visitor.shared_document_ids, [5])
        self.assertIs(self.visitor.stage, stage)
        self.assertEqual(self.visitor.stage_id, "stage-demo")
        self.assertFalse(hasattr(self.visitor, "new_field"))

    def test_language_pass_restores_state_before_exception_handler(self):
        with self.assertRaises(ValueError):
            with preserve_preview_state(self.visitor):
                self.visitor.attributes.clear()
                del self.visitor.preview_reminder
                raise ValueError("fixture")
        self.assertEqual(self.visitor.attributes, {"industry": "Retail"})
        self.assertEqual(self.visitor.preview_reminder, {"title": "Call"})

    def test_nested_preview_scopes_restore_their_own_snapshot(self):
        with preserve_preview_state(self.visitor):
            self.visitor.attributes["industry"] = "Intermediate"
            with preserve_preview_state(self.visitor):
                self.visitor.attributes["industry"] = "Temporary"
            self.assertEqual(self.visitor.attributes["industry"], "Intermediate")
        self.assertEqual(self.visitor.attributes["industry"], "Retail")

    def honest(self, message, *, files=None, events=None, languages=(), request="Please call me tomorrow and share the brochure."):
        return enforce_preview_action_honesty(
            decision=Decision(message=message), files=files or [], events=events or [],
            requested_text=request, allowed_languages=languages,
        ).message

    def test_slow_turn_raw_future_assurance_becomes_preview_only(self):
        result = self.honest(
            "Thank you for sharing your details. I will now proceed to set up the call for tomorrow "
            "at 3 PM India time and share the product brochure with you.",
            files=[{"id": 18}], events=[{"type": "reminder", "status": "preview"}],
        )
        self.assertIn("Thank you for sharing your details.", result)
        self.assertNotIn("I will", result)
        self.assertIn("document is available in this preview", result)
        self.assertIn("no live call is confirmed", result)

    def test_factual_possession_and_decimal_pricing_survive_action_guard(self):
        facts = "We have a brochure describing the ₹2,999.50 monthly plan. I am a product assistant."
        result = self.honest(facts, files=[{"id": 18}])
        self.assertIn(facts, result)

    def test_supported_price_clause_is_retained_when_promise_is_in_same_sentence(self):
        result = self.honest("The plan costs ₹2,999.50 monthly, and I will share the brochure.", files=[{"id": 18}])
        self.assertIn("The plan costs ₹2,999.50 monthly", result)
        self.assertNotIn("I will share", result)

    def test_hinglish_reply_gets_hinglish_preview_wording(self):
        result = self.honest("Plan ₹2,999 monthly hai. Main brochure bhejunga.", files=[{"id": 18}], languages=["Hinglish", "English"])
        self.assertIn("Plan ₹2,999 monthly hai.", result)
        self.assertNotIn("Main brochure bhejunga", result)
        self.assertIn("preview mein available hai", result)

    def test_configured_hindi_preview_does_not_add_english(self):
        result = self.honest("I will send the brochure.", files=[{"id": 18}], languages=["Hindi"])
        self.assertIn("दस्तावेज़", result)
        self.assertNotIn("The document", result)

    def test_other_configured_languages_do_not_get_english_preview_paragraph(self):
        result = self.honest("El plan cuesta 20 euros.", files=[{"id": 18}], languages=["Spanish"])
        self.assertEqual(result, "El plan cuesta 20 euros.")

    def test_no_file_result_cannot_claim_file_preview(self):
        result = self.honest("I'll send the brochure now.")
        self.assertNotIn("I'll send", result)
        self.assertIn("No document was shared", result)
        self.assertNotIn("document is available", result)

    def test_call_only_does_not_invent_a_file_outcome(self):
        result = self.honest("We will call you tomorrow.", events=[{"type": "reminder", "status": "preview"}], request="Please call me tomorrow.")
        self.assertIn("reminder is shown", result)
        self.assertNotIn("document", result)

    def test_truthful_preview_reply_is_not_repeated(self):
        text = "The document is available in this preview. The reminder is simulated in this preview."
        self.assertEqual(self.honest(text, files=[{"id": 18}], events=[{"type": "reminder", "status": "preview"}]), text)

    def test_prior_or_failed_effect_cannot_establish_a_new_preview(self):
        result = self.honest("I have scheduled the call.", events=[{"type": "reminder", "status": "failed"}])
        self.assertNotIn("scheduled the call", result)
        self.assertNotIn("reminder is shown", result)

    def test_created_product_and_shared_pricing_are_facts_not_action_receipts(self):
        facts = "We have created a booking platform for small businesses. We have shared pricing in our brochure. We share the brochure with prospective customers."
        self.assertEqual(self.honest(facts), facts)

    def test_unrelated_negation_does_not_mask_a_call_assurance(self):
        result = self.honest("I will schedule your call, not send the brochure.", events=[{"type": "reminder", "status": "preview"}])
        self.assertNotIn("I will schedule", result)
        self.assertIn("no live call is confirmed", result)

    def test_preview_disclaimer_does_not_mask_a_file_assurance(self):
        result = self.honest("I will send the brochure; this is a Sandbox preview.")
        self.assertNotIn("I will send", result)
        self.assertIn("No document was shared", result)

    def test_negative_action_clause_is_retained(self):
        text = "I will not send the brochure. No call is confirmed."
        self.assertEqual(self.honest(text), text)

    def test_typed_human_handoff_promise_is_not_live_confirmation(self):
        result = self.honest("I will connect you with our team.", request="Please connect me to a human.")
        self.assertNotIn("I will connect", result)
        self.assertIn("no live call or handoff is confirmed", result)


if __name__ == "__main__":
    unittest.main()
