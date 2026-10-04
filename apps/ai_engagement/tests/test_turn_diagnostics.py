from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services import trace_service
from apps.ai_engagement.services.turn_diagnostics import provider_diagnostics, record_failure, summary


class TurnDiagnosticsTests(SimpleTestCase):
    def setUp(self):
        self.token = trace_service.begin_trace(organization=SimpleNamespace(id="org"))
        self.addCleanup(trace_service.flush, reset_token=self.token)

    def test_provider_failure_records_only_codes_without_error_text(self):
        class Rejected(Exception):
            status_code = 400
            body = {"error": {"code": "invalid_json_schema", "param": "text.format.schema",
                              "type": "invalid_request_error", "message": "secret customer content"}}

        @provider_diagnostics
        def generate(self, **kwargs):
            try:
                raise Rejected("sk-secret-do-not-expose")
            except Rejected as exc:
                raise RuntimeError("private provider details") from exc

        with self.assertRaises(RuntimeError):
            generate(None, metadata={"phase": "grounding"})
        diagnostic = summary()
        self.assertIn("grounding/RuntimeError/400/invalid_json_schema", diagnostic)
        self.assertNotIn("secret", diagnostic)
        self.assertNotIn("private", repr(trace_service.current().data))

    def test_success_is_not_reported_as_failure(self):
        @provider_diagnostics
        def generate(self, **kwargs):
            return SimpleNamespace(model="gpt-test")
        generate(None)
        self.assertEqual(summary(), "")

    def test_successful_declined_file_review_is_observable_without_private_content(self):
        trace_service.record("file_decision", {"draft": {
            "candidate_count": 1, "explicit_request": True, "review_status": "declined",
            "selected_count": 0, "validated_selected_count": 0, "graph_selected_count": 0,
            "grounding_status": "approved", "private_reason": "secret user content",
        }})
        trace_service.append("provider", "calls", {"phase": "file_selection_review", "status": "ok", "model": "private-model"})
        diagnostic = summary(SimpleNamespace(files=[]))
        self.assertIn("file/draft/candidates=1/review=declined", diagnostic)
        self.assertIn("file/preview=0", diagnostic)
        self.assertIn("file_selection_review:ok=1", diagnostic)
        self.assertNotIn("secret", diagnostic)
        self.assertNotIn("private", diagnostic)

    def test_final_phase_does_not_hide_draft_selection_and_grounding_drop(self):
        trace_service.record("file_decision", {
            "draft": {"candidate_count": 1, "explicit_request": True, "review_status": "selected",
                      "selected_count": 1, "validated_selected_count": 1, "graph_selected_count": 0,
                      "grounding_status": "rejected"},
            "final": {"candidate_count": 0, "review_status": "final_language_only"},
        })
        diagnostic = summary(SimpleNamespace(files=[]))
        self.assertIn("review=selected/selected=1/validated=1", diagnostic)
        self.assertIn("graph=0/grounding=rejected", diagnostic)
        self.assertIn("file/final/candidates=0/review=final_language_only", diagnostic)

    def test_welcome_review_exposes_only_the_backend_trigger_and_bounded_counts(self):
        trace_service.record("file_decision", {"draft": {
            "candidate_count": 1, "welcome_due": True, "review_trigger": "welcome",
            "review_status": "selected", "selected_count": 1, "document_name": "private name",
        }})
        diagnostic = summary(SimpleNamespace(files=[{}]))
        self.assertIn("review=selected", diagnostic)
        self.assertIn("trigger=welcome", diagnostic)
        self.assertIn("file/preview=1", diagnostic)
        self.assertNotIn("private", diagnostic)

    def test_capture_diagnostic_accepts_only_safe_counters_and_enums(self):
        trace_service.record("qualification_capture", {
            "candidate_count": 4, "accepted_count": 4, "review_status": "reviewed",
            "private_answers": ["secret lead details"],
        })
        diagnostic = summary()
        self.assertIn("capture/candidates=4/review=reviewed/accepted=4", diagnostic)
        self.assertNotIn("secret", diagnostic)
        trace_service.record("qualification_capture", {"candidate_count": "secret", "review_status": "secret"})
        self.assertNotIn("secret", summary())

    def test_irrelevant_zero_capture_skip_keeps_successful_pricing_diagnostics_empty(self):
        for status in ("skipped", "no_candidates"):
            with self.subTest(status=status):
                trace_service.record("qualification_capture", {
                    "candidate_count": 0, "accepted_count": 0, "review_status": status,
                })
                self.assertEqual(summary(), "")

    def test_zero_count_semantic_review_failure_is_still_visible(self):
        trace_service.record("qualification_capture", {
            "candidate_count": 0, "accepted_count": 0, "review_status": "failed",
        })
        self.assertIn("capture/candidates=0/review=failed/accepted=0", summary())

    def test_missing_graph_observation_for_file_request_is_visible(self):
        diagnostic = summary(SimpleNamespace(files=[], message="Please send the product brochure."))
        self.assertIn("file/draft/path=not_observed", diagnostic)
        self.assertIn("file/preview=0", diagnostic)
        self.assertNotIn("product brochure", diagnostic)

    def test_runtime_failure_includes_code_locations_without_exception_text(self):
        try:
            raise RecursionError("private customer content")
        except RecursionError as exc:
            record_failure(exc)
        diagnostic = summary()
        self.assertIn("application/test_turn_diagnostics.py:", diagnostic)
        self.assertNotIn("private", diagnostic)
        self.assertLessEqual(len(diagnostic), 1000)

    def test_nested_sandbox_scope_restores_previous_trace(self):
        from apps.ai_engagement.services.playground import PlaygroundError
        from apps.ai_engagement.services.turn_diagnostics import sandbox_diagnostics
        parent = trace_service.current()

        @sandbox_diagnostics
        def run(self, **kwargs):
            trace_service.record("grounding", approved=False, validation_reason="provider_error")
            raise PlaygroundError("Generation failed.")

        with self.assertRaisesRegex(PlaygroundError, "validation/provider_error"):
            run(None, organization=SimpleNamespace(id="other"))
        self.assertIs(trace_service.current(), parent)
        self.assertEqual(summary(), "")
