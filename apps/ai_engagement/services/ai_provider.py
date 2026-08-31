from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)


class AIProviderError(Exception):
    """
    Base exception for AI provider failures.
    """

    retryable = False


class AIProviderConfigurationError(AIProviderError):
    """
    Local configuration error.

    These failures should not be retried automatically.
    """

    retryable = False


class AIProviderPermanentError(AIProviderError):
    """
    Provider rejected the request permanently.

    Examples:
        - invalid authentication
        - invalid request
        - permission/model access problems
    """

    retryable = False


class AIProviderTransientError(AIProviderError):
    """
    Temporary provider/network failure.

    These failures are safe for Celery retry handling.
    """

    retryable = True


@dataclass(frozen=True)
class AITextResult:
    """
    Normalized text response returned by an AI provider.
    """

    text: str
    model: str


class OpenAIProvider:
    """
    OpenAI provider adapter for SHVYA AI.

    Business services should depend on this adapter rather than
    importing the OpenAI SDK directly.
    """

    DEFAULT_MODEL = "gpt-4.1-nano"

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
            raise AIProviderConfigurationError(
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

        instructions = (
            instructions or ""
        ).strip()

        input_text = (
            input_text or ""
        ).strip()

        if not instructions:
            raise AIProviderConfigurationError(
                "AI instructions cannot be empty."
            )

        if not input_text:
            raise AIProviderConfigurationError(
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

        except RateLimitError as exc:

            raise AIProviderTransientError(
                f"OpenAI rate limit: {exc}"
            ) from exc

        except APIConnectionError as exc:

            raise AIProviderTransientError(
                f"OpenAI connection failure: {exc}"
            ) from exc

        except AuthenticationError as exc:

            raise AIProviderPermanentError(
                f"OpenAI authentication failed: {exc}"
            ) from exc

        except PermissionDeniedError as exc:

            raise AIProviderPermanentError(
                f"OpenAI permission denied: {exc}"
            ) from exc

        except BadRequestError as exc:

            raise AIProviderPermanentError(
                f"OpenAI rejected the request: {exc}"
            ) from exc

        except APIStatusError as exc:

            status_code = getattr(
                exc,
                "status_code",
                None,
            )

            if status_code is not None and status_code >= 500:
                raise AIProviderTransientError(
                    f"OpenAI server error: {exc}"
                ) from exc

            raise AIProviderPermanentError(
                f"OpenAI API error: {exc}"
            ) from exc

        except Exception as exc:

            raise AIProviderTransientError(
                f"Unexpected OpenAI failure: {exc}"
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
            raise AIProviderPermanentError(
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