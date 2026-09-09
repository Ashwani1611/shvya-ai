from __future__ import annotations

import os
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

from apps.ai_engagement.services.credits import (
    AICreditError,
    AICreditService,
    AICreditUnavailableError,
)


class AIProviderError(Exception):
    """Base exception for AI provider failures."""

    retryable = False


class AIProviderConfigurationError(AIProviderError):
    """Local configuration error. Do not retry automatically."""

    retryable = False


class AIProviderPermanentError(AIProviderError):
    """Provider rejected the request permanently. Do not retry."""

    retryable = False


class AIProviderTransientError(AIProviderError):
    """Temporary provider/network failure safe for Celery retry."""

    retryable = True


@dataclass(frozen=True)
class AITextResult:
    text: str
    model: str


class OpenAIProvider:
    """Central direct-OpenAI provider adapter for SHVYA AI.

    The OpenAI SDK's own automatic retries are disabled so SHVYA has exactly one
    retry owner: the Celery task layer, capped at three attempts. Calls are
    bounded by timeout and task-specific output limits. Callers may optionally
    supply a Responses API JSON schema to reduce malformed output/repair calls.
    """

    DEFAULT_MODEL = "gpt-4.1-nano"
    DEFAULT_TIMEOUT_SECONDS = 20.0

    TASK_MODEL_ENV = {
        "engagement": "OPENAI_ENGAGEMENT_MODEL",
        "playground": "OPENAI_ENGAGEMENT_MODEL",
        "qualification": "OPENAI_QUALIFICATION_MODEL",
        "internal_summary": "OPENAI_SUMMARY_MODEL",
        "lead_briefing": "OPENAI_QUALIFICATION_MODEL",
        "bump_up": "OPENAI_ENGAGEMENT_MODEL",
    }

    TASK_MAX_OUTPUT_TOKENS = {
        "engagement": 300,
        "playground": 350,
        "qualification": 500,
        "internal_summary": 300,
        "lead_briefing": 350,
        "bump_up": 200,
    }

    def __init__(
        self,
        *,
        client: OpenAI | None = None,
        model: str | None = None,
    ) -> None:
        api_key = getattr(settings, "OPENAI_API_KEY", "")
        if not api_key:
            raise AIProviderConfigurationError("OPENAI_API_KEY is not configured.")

        try:
            timeout_seconds = float(
                os.getenv("OPENAI_TIMEOUT_SECONDS", self.DEFAULT_TIMEOUT_SECONDS)
            )
        except (TypeError, ValueError):
            timeout_seconds = self.DEFAULT_TIMEOUT_SECONDS
        timeout_seconds = min(max(timeout_seconds, 5.0), 60.0)

        self.client = client or OpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=timeout_seconds,
        )
        self._explicit_model = bool((model or "").strip())
        self.model = (
            model
            or getattr(settings, "OPENAI_AI_MODEL", self.DEFAULT_MODEL)
            or self.DEFAULT_MODEL
        ).strip()

    def _feature(self, metadata: dict[str, str] | None) -> str:
        return AICreditService.feature_from_metadata(metadata)

    def _model_for_metadata(self, metadata: dict[str, str] | None) -> str:
        if self._explicit_model:
            return self.model
        feature = self._feature(metadata)
        env_name = self.TASK_MODEL_ENV.get(feature)
        if not env_name:
            return self.model
        configured = (
            os.getenv(env_name, "")
            or getattr(settings, env_name, "")
            or ""
        ).strip()
        return configured or self.model

    def _max_output_tokens(self, metadata: dict[str, str] | None) -> int:
        feature = self._feature(metadata)
        default = self.TASK_MAX_OUTPUT_TOKENS.get(feature, 600)
        env_name = f"OPENAI_{feature.upper()}_MAX_OUTPUT_TOKENS"
        try:
            configured = int(os.getenv(env_name, default))
        except (TypeError, ValueError):
            configured = default
        return min(max(configured, 100), 2000)

    def _release_credit_reservation(self, reservation) -> None:
        if reservation is None:
            return
        try:
            AICreditService.release(reservation)
        except AICreditError:
            pass

    def _structured_text_config(self, response_schema: dict[str, Any] | None):
        if not response_schema:
            return None
        name = str(response_schema.get("name") or "").strip()
        schema = response_schema.get("schema")
        if not name or not isinstance(schema, dict):
            raise AIProviderConfigurationError(
                "response_schema requires a name and JSON schema object."
            )
        return {
            "format": {
                "type": "json_schema",
                "name": name[:64],
                "schema": schema,
                "strict": bool(response_schema.get("strict", False)),
            }
        }

    def generate_text(
        self,
        *,
        instructions: str,
        input_text: str,
        metadata: dict[str, str] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> AITextResult:
        instructions = (instructions or "").strip()
        input_text = (input_text or "").strip()
        if not instructions:
            raise AIProviderConfigurationError("AI instructions cannot be empty.")
        if not input_text:
            raise AIProviderConfigurationError("AI input cannot be empty.")

        request_model = self._model_for_metadata(metadata)
        request_kwargs: dict[str, Any] = {
            "model": request_model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": self._max_output_tokens(metadata),
        }
        if metadata:
            request_kwargs["metadata"] = metadata
        text_config = self._structured_text_config(response_schema)
        if text_config is not None:
            request_kwargs["text"] = text_config

        reservation = None
        organization_id = (metadata or {}).get("organization_id")
        if organization_id:
            try:
                reservation = AICreditService.reserve_text(
                    organization_id=organization_id,
                    model=request_model,
                    instructions=instructions,
                    input_text=input_text,
                    feature=self._feature(metadata),
                    reference_id=AICreditService.reference_from_metadata(metadata),
                )
            except AICreditUnavailableError as exc:
                raise AIProviderPermanentError(str(exc)) from exc
            except AICreditError as exc:
                raise AIProviderPermanentError(
                    f"Unable to reserve organization AI credits: {exc}"
                ) from exc

        try:
            response = self.client.responses.create(**request_kwargs)
        except RateLimitError as exc:
            self._release_credit_reservation(reservation)
            raise AIProviderTransientError(f"OpenAI rate limit: {exc}") from exc
        except APIConnectionError as exc:
            self._release_credit_reservation(reservation)
            raise AIProviderTransientError(
                f"OpenAI connection failure: {exc}"
            ) from exc
        except AuthenticationError as exc:
            self._release_credit_reservation(reservation)
            raise AIProviderPermanentError(
                f"OpenAI authentication failed: {exc}"
            ) from exc
        except PermissionDeniedError as exc:
            self._release_credit_reservation(reservation)
            raise AIProviderPermanentError(f"OpenAI permission denied: {exc}") from exc
        except BadRequestError as exc:
            self._release_credit_reservation(reservation)
            raise AIProviderPermanentError(
                f"OpenAI rejected the request: {exc}"
            ) from exc
        except APIStatusError as exc:
            self._release_credit_reservation(reservation)
            status_code = getattr(exc, "status_code", None)
            if status_code is not None and status_code >= 500:
                raise AIProviderTransientError(f"OpenAI server error: {exc}") from exc
            raise AIProviderPermanentError(f"OpenAI API error: {exc}") from exc
        except Exception as exc:
            self._release_credit_reservation(reservation)
            raise AIProviderTransientError(f"Unexpected OpenAI failure: {exc}") from exc

        if reservation is not None:
            input_tokens, output_tokens = AICreditService.extract_usage(response)
            try:
                AICreditService.settle(
                    reservation=reservation,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    embedding=False,
                    metadata={
                        "provider": "openai",
                        "provider_model": str(
                            getattr(response, "model", None) or request_model
                        ),
                    },
                )
            except AICreditError as exc:
                raise AIProviderPermanentError(
                    f"OpenAI completed but AI-credit settlement failed: {exc}"
                ) from exc

        output_text = (getattr(response, "output_text", "") or "").strip()
        if not output_text:
            raise AIProviderPermanentError("OpenAI returned an empty response.")

        return AITextResult(
            text=output_text,
            model=getattr(response, "model", None) or request_model,
        )
