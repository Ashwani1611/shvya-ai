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
