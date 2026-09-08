from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from apps.ai_engagement.services.ai_provider import OpenAIProvider


@override_settings(
    OPENAI_API_KEY="test-key",
    OPENAI_AI_MODEL="fallback-model",
)
class OpenAIModelRoutingTests(SimpleTestCase):
    def _client(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            output_text="ok",
            model=None,
        )
        return client

    def _called_model(self, *, task: str, explicit_model: str | None = None) -> str:
        client = self._client()
        provider = OpenAIProvider(
            client=client,
            model=explicit_model,
        )
        provider.generate_text(
            instructions="Follow SHVYA instructions.",
            input_text="Hello",
            metadata={"task": task},
        )
        return client.responses.create.call_args.kwargs["model"]

    @patch.dict(
        os.environ,
        {
            "OPENAI_ENGAGEMENT_MODEL": "engagement-model",
            "OPENAI_QUALIFICATION_MODEL": "qualification-model",
            "OPENAI_SUMMARY_MODEL": "summary-model",
        },
        clear=False,
    )
    def test_routes_supported_tasks_to_specific_models(self):
        self.assertEqual(
            self._called_model(task="engagement"),
            "engagement-model",
        )
        self.assertEqual(
            self._called_model(task="playground"),
            "engagement-model",
        )
        self.assertEqual(
            self._called_model(task="lead_qualification_summary"),
            "qualification-model",
        )
        self.assertEqual(
            self._called_model(task="internal_conversation_summary"),
            "summary-model",
        )

    @patch.dict(
        os.environ,
        {
            "OPENAI_ENGAGEMENT_MODEL": "",
            "OPENAI_QUALIFICATION_MODEL": "",
            "OPENAI_SUMMARY_MODEL": "",
        },
        clear=False,
    )
    def test_falls_back_to_openai_ai_model(self):
        self.assertEqual(
            self._called_model(task="engagement"),
            "fallback-model",
        )
        self.assertEqual(
            self._called_model(task="other"),
            "fallback-model",
        )

    @patch.dict(
        os.environ,
        {"OPENAI_ENGAGEMENT_MODEL": "engagement-model"},
        clear=False,
    )
    def test_explicit_provider_model_overrides_task_routing(self):
        self.assertEqual(
            self._called_model(
                task="engagement",
                explicit_model="explicit-model",
            ),
            "explicit-model",
        )
