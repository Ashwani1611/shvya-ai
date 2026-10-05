from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from django.test import SimpleTestCase, override_settings
from openai import RateLimitError

from apps.ai_engagement.services import trace_service
from apps.ai_engagement.services.ai_provider import (
    AIProviderQuotaError, AIProviderTransientError, OpenAIProvider,
)


@override_settings(OPENAI_API_KEY="unit-test-unused-key")
class ProviderQuotaTests(SimpleTestCase):
    def setUp(self):
        token = trace_service.begin_trace(organization=SimpleNamespace(id="org"))
        self.addCleanup(trace_service.flush, reset_token=token)
        self.client = Mock()
        self.provider = OpenAIProvider(client=self.client)

    def rejected(self, body):
        self.client.responses.create.side_effect = RateLimitError(
            "Provider details must not be shown", body=body,
            response=httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/responses")),
        )

    def test_quota_does_not_retry_or_make_recovery_calls(self):
        self.rejected({"code": "credit_balance_exhausted", "type": "insufficient_quota"})
        for _ in range(2):
            with self.assertRaises(AIProviderQuotaError) as raised:
                self.provider.generate_text(instructions="Answer.", input_text="Hi")
            self.assertFalse(raised.exception.retryable)
        self.client.responses.create.assert_called_once()

    def test_real_rate_limit_remains_retryable(self):
        self.rejected({"code": "rate_limit_exceeded", "type": "requests"})
        with self.assertRaises(AIProviderTransientError) as raised:
            self.provider.generate_text(instructions="Answer.", input_text="Hi")
        self.assertTrue(raised.exception.retryable)

    def test_nested_quota_payload_is_permanent(self):
        self.rejected({"error": {"code": "insufficient_quota"}})
        with self.assertRaises(AIProviderQuotaError):
            self.provider.generate_text(instructions="Answer.", input_text="Hi")

    def test_token_rate_limit_message_exposes_retry_after(self):
        self.rejected({
            "error": {
                "code": "rate_limit_exceeded",
                "type": "tokens",
                "message": "Please try again in 3.675s.",
            }
        })
        with self.assertRaises(AIProviderTransientError) as raised:
            self.provider.generate_text(instructions="Answer.", input_text="Hi")
        self.assertEqual(raised.exception.retry_after, 3)

    @patch.dict("os.environ", {"OPENAI_MAX_PROMPT_TOKENS": "4000"})
    def test_oversized_prompt_is_compacted_before_provider_call(self):
        self.client.responses.create.return_value = SimpleNamespace(
            output_text="ok",
            model="gpt-4.1-nano",
            usage=None,
            status="completed",
        )
        result = self.provider.generate_text(
            instructions="RULES " + ("x" * 30000),
            input_text="TURN " + ("y" * 30000),
            metadata={"task": "engagement"},
        )
        self.assertEqual(result.text, "ok")
        kwargs = self.client.responses.create.call_args.kwargs
        self.assertLessEqual(
            len(kwargs["instructions"]) + len(kwargs["input"]),
            4000 * 4,
        )
        self.assertIn(
            "compacted",
            kwargs["instructions"] + kwargs["input"],
        )
