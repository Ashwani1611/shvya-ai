from __future__ import annotations

import copy
import logging
import os
import time
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
from apps.core.observability import emit_event, increment, observe_latency

from apps.ai_engagement.services.turn_diagnostics import provider_diagnostics

logger = logging.getLogger(__name__)


class AIProviderError(Exception):
    """Base exception for AI provider failures."""

    retryable = False


class AIProviderConfigurationError(AIProviderError):
    """Local configuration error. Do not retry automatically."""

    retryable = False


class AIProviderPermanentError(AIProviderError):
    """Provider rejected the request permanently. Do not retry."""

    retryable = False


class AIProviderQuotaError(AIProviderPermanentError):
    """Provider billing needs operator action; retrying cannot restore credits."""


def is_provider_quota_error(error):
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(detail, dict):
        return False
    return bool({str(detail.get("code") or ""), str(detail.get("type") or "")}
                & {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"})


class AIProviderTransientError(AIProviderError):
    """Temporary provider/network failure safe for Celery retry."""

    retryable = True

    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        try:
            self.retry_after = (
                max(1, min(int(float(retry_after)), 900))
                if retry_after is not None
                else None
            )
        except (TypeError, ValueError):
            self.retry_after = None


def _provider_retry_after(error):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    raw = str(headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(1, min(int(float(raw)), 900))
    except (TypeError, ValueError):
        return None


def provider_retry_countdown(
    error,
    *,
    identifier,
    retries,
    default=30,
    maximum=300,
):
    """Bound exponential provider retry timing and spread fleet retries."""
    try:
        retry_count = max(0, int(retries or 0))
    except (TypeError, ValueError):
        # Celery normally supplies an int. Tests and defensive wrappers can
        # expose mock/non-numeric request objects; treat them as the first retry.
        retry_count = 0

    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        try:
            base = max(1, min(int(float(retry_after)), 900))
        except (TypeError, ValueError):
            base = int(default)
    else:
        base = min(
            max(1, int(maximum)),
            max(1, int(default)) * (2 ** retry_count),
        )
    jitter = (
        sum(ord(character) for character in str(identifier))
        + (retry_count * 19)
    ) % 7
    return min(907, base + jitter)


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
        "engagement": 700,
        "playground": 700,
        "qualification": 500,
        "internal_summary": 300,
        "lead_briefing": 350,
        "bump_up": 200,
        "call_intelligence": 2000,
    }

    def __init__(
        self,
        *,
        client: OpenAI | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        api_key = getattr(settings, "OPENAI_API_KEY", "")
        if not api_key:
            raise AIProviderConfigurationError("OPENAI_API_KEY is not configured.")

        try:
            timeout_seconds = float(timeout_seconds if timeout_seconds is not None else
                                    os.getenv("OPENAI_TIMEOUT_SECONDS", self.DEFAULT_TIMEOUT_SECONDS))
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

        # Internal routing metadata is produced only inside this provider. It is
        # removed before metadata is sent to OpenAI and is never accepted from
        # organization-facing configuration surfaces.
        routed = str((metadata or {}).get("__routing_model") or "").strip()
        if routed:
            if len(routed) > 100 or any(ch.isspace() for ch in routed):
                raise AIProviderConfigurationError("Invalid internally routed model.")
            return routed

        override = str((metadata or {}).get("model_override") or "").strip()
        if override:
            if len(override) > 100 or any(ch.isspace() for ch in override):
                raise AIProviderConfigurationError("Invalid organization model override.")
            return override
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

    def _model_for_request(
        self,
        *,
        metadata: dict[str, str] | None,
        input_text: str,
    ) -> tuple[str, str, str]:
        metadata = metadata or {}

        # Explicit client construction and Superadmin-controlled organization
        # overrides remain authoritative. Adaptive routing is a platform
        # optimization only when no fixed override has been selected.
        if self._explicit_model:
            return self.model, "fixed", "explicit_provider_model"
        if str(metadata.get("__routing_model") or "").strip():
            return (
                self._model_for_metadata(metadata),
                str(metadata.get("__routing_tier") or "fallback"),
                str(metadata.get("__routing_reason") or "provider_fallback"),
            )
        if str(metadata.get("model_override") or "").strip():
            return self._model_for_metadata(metadata), "fixed", "superadmin_override"

        base_model = self._model_for_metadata(metadata)
        from apps.ai_engagement.services.model_router import route_model

        route = route_model(
            base_model=base_model,
            input_text=input_text,
            metadata=metadata,
        )
        return route.model, route.tier, route.reason

    @staticmethod
    def _provider_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
        return {
            str(key): str(value)
            for key, value in (metadata or {}).items()
            if not str(key).startswith("__") and value is not None
        }

    def _fallback_model(
        self,
        *,
        request_model: str,
        routing_tier: str,
        metadata: dict[str, str] | None,
    ) -> str:
        attempted = {
            item.strip()
            for item in str((metadata or {}).get("__attempted_models") or "").split(",")
            if item.strip()
        }
        attempted.add(str(request_model or "").strip())

        fallback_metadata = self._metadata_without_model_override(metadata)
        fallback_metadata.pop("__routing_model", None)
        fallback_metadata.pop("__routing_tier", None)
        fallback_metadata.pop("__routing_reason", None)
        platform_base = self._model_for_metadata(fallback_metadata)

        from apps.ai_engagement.services.model_router import fallback_models
        from apps.ai_engagement.services.provider_resilience import is_open

        for candidate in fallback_models(
            primary_model=request_model,
            base_model=platform_base,
            tier=routing_tier,
        ):
            if candidate not in attempted and not is_open(candidate):
                return candidate
        return ""

    def _retry_on_fallback_model(
        self,
        *,
        instructions: str,
        input_text: str,
        metadata: dict[str, str] | None,
        response_schema: dict[str, Any] | None,
        request_model: str,
        routing_tier: str,
        reason: str,
    ) -> AITextResult | None:
        if str((metadata or {}).get("__fallback_attempted") or "") == "1":
            return None

        fallback_model = self._fallback_model(
            request_model=request_model,
            routing_tier=routing_tier,
            metadata=metadata,
        )
        if not fallback_model:
            return None

        organization_id = (metadata or {}).get("organization_id")
        attempted = [
            item.strip()
            for item in str((metadata or {}).get("__attempted_models") or "").split(",")
            if item.strip()
        ]
        attempted.append(request_model)

        increment(
            "ai.provider_model_fallbacks",
            labels={"provider": "openai", "reason": reason},
        )
        emit_event(
            "ai.provider.model_fallback",
            provider="openai",
            organization_id=organization_id,
            requested_model=request_model,
            fallback_model=fallback_model,
            reason=reason,
        )
        try:
            from apps.ai_engagement.services.trace_service import record

            record(
                "provider",
                {
                    "fallback_used": True,
                    "fallback_reason": reason,
                    "primary_model": request_model,
                    "fallback_model": fallback_model,
                },
            )
        except Exception:
            pass

        fallback_metadata = {
            **(metadata or {}),
            "__routing_model": fallback_model,
            "__routing_tier": "fallback",
            "__routing_reason": reason,
            "__fallback_attempted": "1",
            "__attempted_models": ",".join(dict.fromkeys(attempted)),
        }
        return self.generate_text(
            instructions=instructions,
            input_text=input_text,
            metadata=fallback_metadata,
            response_schema=response_schema,
        )

    @staticmethod
    def _model_override_unavailable(
        error,
        *,
        metadata: dict[str, str] | None,
        request_model: str,
    ) -> bool:
        """Return True only when an org-level model override itself is unusable."""

        override = str((metadata or {}).get("model_override") or "").strip()
        if not override or override != request_model:
            return False
        message = str(error or "").casefold()
        if "model" not in message:
            return False
        return any(
            marker in message
            for marker in (
                "does not exist",
                "not found",
                "model_not_found",
                "do not have access",
                "don't have access",
                "not have access",
                "unsupported model",
                "is not supported",
                "permission",
            )
        )

    def _metadata_without_model_override(
        self,
        metadata: dict[str, str] | None,
    ) -> dict[str, str]:
        fallback = dict(metadata or {})
        fallback.pop("model_override", None)
        return fallback

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
            increment("ai.credit_release_failures")
            pass

    def _strict_engagement_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Make the engagement contract enforceable by Structured Outputs.

        The engagement service validates exact CRM and qualification action
        shapes after generation. Keep the provider schema aligned with those
        validators so malformed model output cannot strand playground, API, or
        hosted-account engagement before the transport layer is reached.
        """
        hardened = copy.deepcopy(schema)
        properties = hardened.get("properties")
        if not isinstance(properties, dict):
            raise AIProviderConfigurationError(
                "engagement response schema requires object properties."
            )

        scalar_value = {
            "anyOf": [
                {"type": ["string", "number", "boolean", "null"]},
                {
                    "type": "array",
                    "items": {"type": ["string", "number", "boolean", "null"]},
                },
            ]
        }
        existing_update_item = {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": scalar_value,
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        }
        dynamic_update_item = {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": scalar_value,
                "name": {"type": "string"},
                "field_type": {
                    "type": "string",
                    "enum": ["text", "numeric", "date", "datetime"],
                },
                "create_if_missing": {"type": "boolean"},
            },
            "required": [
                "key",
                "value",
                "name",
                "field_type",
                "create_if_missing",
            ],
            "additionalProperties": False,
        }
        update_item = {
            "anyOf": [
                existing_update_item,
                dynamic_update_item,
            ]
        }
        contact_item = {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "channel": {"type": "string"},
                "handle": {"type": "string"},
            },
            "required": ["contact_id", "channel", "handle"],
            "additionalProperties": False,
        }
        action_schemas = [
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["attribute_updates"]},
                    "updates": {"type": "array", "items": update_item},
                },
                "required": ["type", "updates"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["pipeline_transition"]},
                    "stage_shift": {
                        "type": "object",
                        "properties": {"stage_id": {"type": "string"}},
                        "required": ["stage_id"],
                        "additionalProperties": False,
                    },
                },
                "required": ["type", "stage_shift"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["add_note"]},
                    "note": {"type": "string"},
                },
                "required": ["type", "note"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["create_reminder"]},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "due_at": {"type": "string"},
                },
                "required": ["type", "title", "description", "due_at"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["contact_updates"]},
                    "updates": {"type": "array", "items": contact_item},
                },
                "required": ["type", "updates"],
                "additionalProperties": False,
            },
        ]
        properties["crm_actions"] = {
            "type": "array",
            "items": {"anyOf": action_schemas},
        }
        properties["qualification_updates"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "value": scalar_value,
                    "source_message_id": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "requirement_id",
                    "value",
                    "source_message_id",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        }
        return hardened

    def _structured_text_config(self, response_schema: dict[str, Any] | None):
        if not response_schema:
            return None
        name = str(response_schema.get("name") or "").strip()
        schema = response_schema.get("schema")
        if not name or not isinstance(schema, dict):
            raise AIProviderConfigurationError(
                "response_schema requires a name and JSON schema object."
            )

        strict = bool(response_schema.get("strict", False))
        if name == "shvya_engagement_decision":
            schema = self._strict_engagement_schema(schema)
            strict = True

        return {
            "format": {
                "type": "json_schema",
                "name": name[:64],
                "schema": schema,
                "strict": strict,
            }
        }

    @provider_diagnostics
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

        request_model, routing_tier, routing_reason = self._model_for_request(
            metadata=metadata,
            input_text=input_text,
        )
        try:
            from apps.ai_engagement.services.trace_service import record

            record(
                "provider",
                {
                    "routing_tier": routing_tier,
                    "routing_reason": routing_reason,
                    "requested_model": request_model,
                },
            )
        except Exception:
            pass

        from apps.ai_engagement.services.provider_resilience import is_open

        if is_open(request_model):
            try:
                from apps.ai_engagement.services.trace_service import record

                record(
                    "provider",
                    {
                        "circuit_breaker_bypassed_primary": True,
                        "primary_model": request_model,
                    },
                )
            except Exception:
                pass
            fallback = self._retry_on_fallback_model(
                instructions=instructions,
                input_text=input_text,
                metadata=metadata,
                response_schema=response_schema,
                request_model=request_model,
                routing_tier=routing_tier,
                reason="circuit_open",
            )
            if fallback is not None:
                return fallback
            raise AIProviderTransientError(
                "OpenAI model circuit breaker is temporarily open."
            )

        request_kwargs: dict[str, Any] = {
            "model": request_model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": self._max_output_tokens(metadata),
        }
        # GPT-5 mini/nano share their output cap with hidden reasoning. The
        # short non-reasoning reply cap can otherwise expire before any JSON.
        reasoning_model = request_model in {"gpt-5-mini", "gpt-5-nano"} or request_model.startswith(("gpt-5-mini-", "gpt-5-nano-"))
        if reasoning_model:
            request_kwargs["reasoning"] = {"effort": "minimal"}
            request_kwargs["max_output_tokens"] = max(request_kwargs["max_output_tokens"], 2000)
        provider_metadata = self._provider_metadata(metadata)
        if provider_metadata:
            request_kwargs["metadata"] = provider_metadata
        text_config = self._structured_text_config(response_schema)
        if text_config is not None:
            request_kwargs["text"] = text_config
            # Older deployments configured 300 tokens for a short chat reply.
            # Engagement now includes evidence and CRM actions in that same JSON
            # envelope; truncation must not strand the conversation at extraction.
            if (response_schema or {}).get("name") == "shvya_engagement_decision":
                minimum = 1400 if (metadata or {}).get("phase") == "schema_repair" else 700
                request_kwargs["max_output_tokens"] = max(
                    request_kwargs["max_output_tokens"], minimum,
                )

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
                    output_token_limit=request_kwargs["max_output_tokens"],
                )
            except AICreditUnavailableError as exc:
                increment("ai.credit_reservation_failures", labels={"reason": "unavailable"})
                raise AIProviderPermanentError(str(exc)) from exc
            except AICreditError as exc:
                increment("ai.credit_reservation_failures", labels={"reason": "error"})
                raise AIProviderPermanentError(
                    f"Unable to reserve organization AI credits: {exc}"
                ) from exc

        provider_started = time.perf_counter()
        provider_error = ""
        try:
            response = self.client.responses.create(**request_kwargs)
        except RateLimitError as exc:
            exhausted = is_provider_quota_error(exc)
            provider_error = "quota_exhausted" if exhausted else "rate_limit"
            self._release_credit_reservation(reservation)
            if exhausted:
                raise AIProviderQuotaError("OpenAI API billing quota is exhausted.") from exc

            from apps.ai_engagement.services.provider_resilience import record_transient_failure

            record_transient_failure(request_model)
            increment(
                "ai.provider_throttled",
                labels={"provider": "openai"},
            )
            fallback = self._retry_on_fallback_model(
                instructions=instructions,
                input_text=input_text,
                metadata=metadata,
                response_schema=response_schema,
                request_model=request_model,
                routing_tier=routing_tier,
                reason="rate_limit",
            )
            if fallback is not None:
                return fallback
            raise AIProviderTransientError(
                f"OpenAI rate limit: {exc}",
                retry_after=_provider_retry_after(exc),
            ) from exc
        except APIConnectionError as exc:
            provider_error = "connection"
            self._release_credit_reservation(reservation)
            from apps.ai_engagement.services.provider_resilience import record_transient_failure

            record_transient_failure(request_model)
            fallback = self._retry_on_fallback_model(
                instructions=instructions,
                input_text=input_text,
                metadata=metadata,
                response_schema=response_schema,
                request_model=request_model,
                routing_tier=routing_tier,
                reason="connection",
            )
            if fallback is not None:
                return fallback
            raise AIProviderTransientError(
                f"OpenAI connection failure: {exc}"
            ) from exc
        except AuthenticationError as exc:
            provider_error = "authentication"
            self._release_credit_reservation(reservation)
            raise AIProviderPermanentError(
                f"OpenAI authentication failed: {exc}"
            ) from exc
        except PermissionDeniedError as exc:
            provider_error = "permission"
            self._release_credit_reservation(reservation)
            if self._model_override_unavailable(
                exc,
                metadata=metadata,
                request_model=request_model,
            ):
                fallback_metadata = self._metadata_without_model_override(metadata)
                fallback_model = self._model_for_metadata(fallback_metadata)
                if fallback_model != request_model:
                    increment(
                        "ai.model_override_fallbacks",
                        labels={"provider": "openai", "reason": "permission"},
                    )
                    emit_event(
                        "ai.model_override.fallback",
                        provider="openai",
                        organization_id=organization_id,
                        requested_model=request_model,
                        fallback_model=fallback_model,
                    )
                    return self.generate_text(
                        instructions=instructions,
                        input_text=input_text,
                        metadata=fallback_metadata,
                        response_schema=response_schema,
                    )
            raise AIProviderPermanentError(f"OpenAI permission denied: {exc}") from exc
        except BadRequestError as exc:
            provider_error = "bad_request"
            self._release_credit_reservation(reservation)
            if self._model_override_unavailable(
                exc,
                metadata=metadata,
                request_model=request_model,
            ):
                fallback_metadata = self._metadata_without_model_override(metadata)
                fallback_model = self._model_for_metadata(fallback_metadata)
                if fallback_model != request_model:
                    increment(
                        "ai.model_override_fallbacks",
                        labels={"provider": "openai", "reason": "bad_request"},
                    )
                    emit_event(
                        "ai.model_override.fallback",
                        provider="openai",
                        organization_id=organization_id,
                        requested_model=request_model,
                        fallback_model=fallback_model,
                    )
                    return self.generate_text(
                        instructions=instructions,
                        input_text=input_text,
                        metadata=fallback_metadata,
                        response_schema=response_schema,
                    )
            raise AIProviderPermanentError(
                f"OpenAI rejected the request: {exc}"
            ) from exc
        except APIStatusError as exc:
            provider_error = "api_status"
            self._release_credit_reservation(reservation)
            status_code = getattr(exc, "status_code", None)
            if (
                status_code is not None
                and status_code < 500
                and self._model_override_unavailable(
                    exc,
                    metadata=metadata,
                    request_model=request_model,
                )
            ):
                fallback_metadata = self._metadata_without_model_override(metadata)
                fallback_model = self._model_for_metadata(fallback_metadata)
                if fallback_model != request_model:
                    increment(
                        "ai.model_override_fallbacks",
                        labels={"provider": "openai", "reason": "api_status"},
                    )
                    emit_event(
                        "ai.model_override.fallback",
                        provider="openai",
                        organization_id=organization_id,
                        requested_model=request_model,
                        fallback_model=fallback_model,
                    )
                    return self.generate_text(
                        instructions=instructions,
                        input_text=input_text,
                        metadata=fallback_metadata,
                        response_schema=response_schema,
                    )
            if status_code is not None and status_code >= 500:
                from apps.ai_engagement.services.provider_resilience import record_transient_failure

                record_transient_failure(request_model)
                fallback = self._retry_on_fallback_model(
                    instructions=instructions,
                    input_text=input_text,
                    metadata=metadata,
                    response_schema=response_schema,
                    request_model=request_model,
                    routing_tier=routing_tier,
                    reason="server_error",
                )
                if fallback is not None:
                    return fallback
                raise AIProviderTransientError(
                    f"OpenAI server error: {exc}",
                    retry_after=_provider_retry_after(exc),
                ) from exc
            raise AIProviderPermanentError(f"OpenAI API error: {exc}") from exc
        except Exception as exc:
            provider_error = "unexpected"
            self._release_credit_reservation(reservation)
            from apps.ai_engagement.services.provider_resilience import record_transient_failure

            record_transient_failure(request_model)
            fallback = self._retry_on_fallback_model(
                instructions=instructions,
                input_text=input_text,
                metadata=metadata,
                response_schema=response_schema,
                request_model=request_model,
                routing_tier=routing_tier,
                reason="unexpected",
            )
            if fallback is not None:
                return fallback
            raise AIProviderTransientError(f"Unexpected OpenAI failure: {exc}") from exc
        finally:
            observe_latency(
                "ai.provider_latency_ms",
                (time.perf_counter() - provider_started) * 1000,
                labels={"provider": "openai", "feature": self._feature(metadata)},
            )
            if provider_error:
                increment(
                    "ai.provider_errors",
                    labels={"provider": "openai", "reason": provider_error},
                )
                emit_event(
                    "ai.provider.failed",
                    provider="openai",
                    reason=provider_error,
                    organization_id=organization_id,
                )
            else:
                increment(
                    "ai.provider_calls",
                    labels={"provider": "openai", "feature": self._feature(metadata)},
                )

        from apps.ai_engagement.services.provider_resilience import record_success

        record_success(request_model)
        try:
            from apps.ai_engagement.services.trace_service import record

            record(
                "provider",
                {
                    "completed_model": str(
                        getattr(response, "model", None) or request_model
                    ),
                    "provider_success": True,
                },
            )
        except Exception:
            pass

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
            except Exception:
                increment("ai.credit_settlement_failures")
                # The provider already completed successfully. Do not discard a
                # valid customer response because an internal ledger write failed.
                # Keep the reservation active so its credits remain unavailable,
                # persist provider usage for recovery, and continue the response.
                try:
                    AICreditService.mark_settlement_pending(
                        reservation=reservation,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        embedding=False,
                    )
                except Exception:
                    logger.exception(
                        "AI credit settlement and recovery marker both failed for "
                        "reservation %s",
                        getattr(reservation, "id", reservation),
                    )
                else:
                    logger.exception(
                        "AI credit settlement deferred for reservation %s",
                        getattr(reservation, "id", reservation),
                    )

        # Never send partial JSON or mistake token exhaustion for a permanent
        # configuration failure. Usage above is settled even for incomplete
        # responses. Existing channel task retry budgets own recovery.
        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
            if reason == "max_output_tokens":
                fallback_metadata = self._metadata_without_model_override(metadata)
                if (metadata or {}).get("model_override") and self._model_for_metadata(fallback_metadata) != request_model:
                    # Text generation has no transport side effect. Retry once
                    # through the platform model, preserving the admin's choice.
                    return self.generate_text(
                        instructions=instructions, input_text=input_text,
                        metadata=fallback_metadata, response_schema=response_schema,
                    )
                raise AIProviderTransientError("OpenAI response exhausted its output token budget.")
            raise AIProviderPermanentError("OpenAI returned an incomplete response.")

        output_text = (getattr(response, "output_text", "") or "").strip()
        if not output_text:
            raise AIProviderPermanentError("OpenAI returned an empty response.")

        return AITextResult(
            text=output_text,
            model=getattr(response, "model", None) or request_model,
        )
