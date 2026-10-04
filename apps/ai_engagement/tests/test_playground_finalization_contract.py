"""Credential-free contracts for projections, not live-model accuracy tests."""
from dataclasses import dataclass, field
from types import SimpleNamespace
import unittest

from apps.ai_engagement.services.playground_finalization import (
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


if __name__ == "__main__":
    unittest.main()
