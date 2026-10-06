from django.test import SimpleTestCase

from apps.ai_engagement.services.playground import PlaygroundResult


class SandboxPublicDiagnosticsTests(SimpleTestCase):
    def test_client_payload_omits_internal_diagnostics_but_retains_trace_reference(self):
        result = PlaygroundResult(
            session_id="test", message="Hello", response="Welcome",
            should_engage=True, knowledge=[], model="test",
            diagnostics="validation/unanswered_question",
            trace_id="trace-reference",
        )
        payload = result.as_dict()
        self.assertNotIn("diagnostics", payload)
        self.assertEqual(payload["response"], "Welcome")
        self.assertEqual(payload["trace_id"], "trace-reference")
        self.assertEqual(result.diagnostics, "validation/unanswered_question")
