from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from openai import OpenAI


class AIProviderError(Exception):
    """
    Raised when an AI provider cannot produce a valid result.
    """


@dataclass(frozen=True)
class AITextResult:
    """
    Normalized result returned by an AI provider.
    """

    text: str
    model: str


class OpenAIProvider:
    """
    OpenAI provider adapter for SHVYA AI.

    Application services should depend on this abstraction
    rather than importing the OpenAI SDK directly.

    This keeps provider-specific implementation isolated.
    """

    DEFAULT_MODEL = "gpt-5.4"

    def __init__(
        self,
        *,
        client: OpenAI | None = None,
        model: str | None = None,
    ) -> None:

        api_key = getattr(
            settings,
            "OPENAI_API_KEY",
            "",
        )

        if not api_key:
            raise AIProviderError(
                "OPENAI_API_KEY is not configured."
            )

        self.client = (
            client
            or OpenAI(
                api_key=api_key,
            )
        )

        self.model = (
            model
            or getattr(
                settings,
                "OPENAI_AI_MODEL",
                self.DEFAULT_MODEL,
            )
        )

    def generate_text(
        self,
        *,
        instructions: str,
        input_text: str,
        metadata: dict[str, str] | None = None,
    ) -> AITextResult:
        """
        Generate text using the OpenAI Responses API.
        """

        if not instructions.strip():
            raise AIProviderError(
                "AI instructions cannot be empty."
            )

        if not input_text.strip():
            raise AIProviderError(
                "AI input cannot be empty."
            )

        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_text,
        }

        if metadata:
            request_kwargs["metadata"] = metadata

        try:

            response = (
                self.client.responses.create(
                    **request_kwargs,
                )
            )

        except Exception as exc:

            raise AIProviderError(
                f"OpenAI request failed: {exc}"
            ) from exc

        output_text = (
            getattr(
                response,
                "output_text",
                "",
            )
            or ""
        ).strip()

        if not output_text:
            raise AIProviderError(
                "OpenAI returned an empty response."
            )

        return AITextResult(
            text=output_text,
            model=(
                getattr(
                    response,
                    "model",
                    None,
                )
                or self.model
            ),
        )