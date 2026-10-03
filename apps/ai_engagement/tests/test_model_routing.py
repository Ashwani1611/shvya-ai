from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

import apps.ai_engagement.services.ai_provider as ai_provider_module
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

    @patch.dict(
        os.environ,
        {"OPENAI_ENGAGEMENT_MODEL": "platform-engagement-model"},
        clear=False,
    )
    def test_unavailable_org_override_falls_back_to_platform_model(self):
        class FakeBadRequest(Exception):
            pass

        client = self._client()
        client.responses.create.side_effect = [
            FakeBadRequest(
                "The model 'retired-org-model' does not exist or you do not have access to it."
            ),
            SimpleNamespace(output_text="fallback ok", model="platform-engagement-model"),
        ]
        provider = OpenAIProvider(client=client)

        with patch.object(ai_provider_module, "BadRequestError", FakeBadRequest):
            result = provider.generate_text(
                instructions="Follow SHVYA instructions.",
                input_text="Hello",
                metadata={
                    "task": "engagement",
                    "model_override": "retired-org-model",
                },
            )

        self.assertEqual(result.text, "fallback ok")
        self.assertEqual(result.model, "platform-engagement-model")
        self.assertEqual(client.responses.create.call_count, 2)
        self.assertEqual(
            client.responses.create.call_args_list[0].kwargs["model"],
            "retired-org-model",
        )
        self.assertEqual(
            client.responses.create.call_args_list[1].kwargs["model"],
            "platform-engagement-model",
        )

