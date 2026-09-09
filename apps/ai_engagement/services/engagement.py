from __future__ import annotations

import json
import os
import re
import logging
from dataclasses import dataclass, field
from typing import Any
from redis.exceptions import RedisError

from django.utils import timezone
from django.core.cache import cache

from apps.ai_engagement.prompts.engagement import (
    CUSTOMER_ENGAGEMENT_INSTRUCTIONS,
)
from apps.ai_engagement.services.ai_provider import (
    AIProviderError,
    AIProviderTransientError,
    AITextResult,
    OpenAIProvider,
)
from apps.ai_engagement.services.base_instructions import (
    SHVYABaseInstructions,
)
from apps.ai_engagement.services.context import (
    AIContext,
    AIContextBuilder,
)
from apps.ai_engagement.services.crm_actions import (
    ALLOWED_ACTION_TYPES,
    CRMActionSchemaError,
    validate_crm_actions,
)
from apps.ai_engagement.services.engagement_lock import (
    EngagementGenerationLock,
    EngagementLockError,
)
from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
)
from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION,
    REQUIREMENT_ANSWERED,
    apply_unambiguous_reply,
    next_requirement,
    state_for_lead,
    project_answer_updates,
)


class EngagementError(Exception):
    """Raised when AI Engagement cannot safely produce a decision."""


REASON_CODES = {
    "ANSWER_ORG_QUESTION",
    "QUALIFICATION_NEXT",
    "QUALIFICATION_CLARIFY",
    "NORMAL_CONVERSATION",
    "HUMAN_HANDOFF",
    "OPT_OUT",
    "UNKNOWN_INFORMATION",
    "NO_ACTION",
}

ENGAGEMENT_RESPONSE_SCHEMA = {
    "name": "shvya_engagement_decision",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "should_engage": {"type": "boolean"},
            "message": {"type": "string"},
            "file_document_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "crm_actions": {"type": "array", "items": {"type": "object"}},
            "qualification_updates": {"type": "array", "items": {"type": "object"}},
            "next_requirement_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "reason_code": {"type": "string", "enum": sorted(REASON_CODES)},
        },
        "required": [
            "should_engage",
            "message",
            "file_document_id",
            "crm_actions",
            "qualification_updates",
            "next_requirement_id",
            "reason_code",
        ],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class EngagementDecision:
    """Normalized AI proposal. Side effects are executed elsewhere."""

    should_engage: bool
    message: str
    file_document_id: int | None
    crm_actions: list[dict[str, Any]]
    reason: str
    model: str
    next_requirement_id: str | None = None
    reason_code: str = ""
    qualification_updates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "should_engage": self.should_engage,
            "message": self.message,
            "file_document_id": self.file_document_id,
            "crm_actions": self.crm_actions,
            # Keep reason for backwards-compatible audit/UI callers while new
            # orchestration uses the bounded reason_code enum.
            "reason": self.reason,
            "reason_code": self.reason_code or self.reason,
            "next_requirement_id": self.next_requirement_id,
            "model": self.model,
            "qualification_updates": self.qualification_updates,
        }


class EngagementService:
    """Precise one-call customer engagement orchestration.

    Python owns permissions, qualification state, deterministic next-requirement
    selection, CRM validation, stage transitions, idempotency and sending. The
    model handles only language understanding/generation that benefits from an
    LLM. Obvious replies to an explicitly authored previous qualification
    question can use a zero-LLM path.
    """

    ENGAGEMENT_TASK_INSTRUCTIONS = CUSTOMER_ENGAGEMENT_INSTRUCTIONS
    ALLOWED_ACTION_TYPES = ALLOWED_ACTION_TYPES
    ALLOWED_PIPELINE_TRANSITION_TYPES = {"stage_shift"}

    MESSAGE_LIMIT = 12
    KNOWLEDGE_LIMIT = 3
    NOTE_LIMIT = 5
    DEFAULT_RECENT_CONVERSATION_CHARS = 6000

    _SIMPLE_ACKS = {
        "yes",
        "yeah",
        "yep",
        "no",
        "nope",
        "ok",
        "okay",
        "sure",
        "done",
        "correct",
        "right",
        "maybe",
        "not sure",
        "interested",
        "thanks",
        "thank you",
    }

    _KNOWLEDGE_TERMS = {
        "price",
        "pricing",
        "cost",
        "fee",
        "fees",
        "plan",
        "plans",
        "package",
        "packages",
        "service",
        "services",
        "feature",
        "features",
        "include",
        "included",
        "policy",
        "refund",
        "guarantee",
        "location",
        "address",
        "course",
        "courses",
        "product",
        "products",
        "availability",
        "available",
        "recommend",
        "suggest",
        "difference",
        "brochure",
        "website",
        "offer",
        "offers",
    }

    def __init__(
        self,
        *,
        provider: OpenAIProvider | None = None,
        context_builder: AIContextBuilder | None = None,
    ) -> None:
        self.provider = provider
        self.context_builder = context_builder or AIContextBuilder()

    def engage(
        self,
        *,
        organization,
        lead,
        knowledge_query: str | None = None,
        context: AIContext | None = None,
    ) -> EngagementDecision:
        if organization is None:
            raise EngagementError("Organization is required.")
        if lead is None:
            raise EngagementError("Lead is required.")

        caller_supplied_context = context is not None
        if context is None:
            context = self.context_builder.build(
                organization=organization,
                lead=lead,
                knowledge_query=None,
                message_limit=self.MESSAGE_LIMIT,
                knowledge_limit=self.KNOWLEDGE_LIMIT,
                note_limit=self.NOTE_LIMIT,
            )

        self._validate_context_scope(
            organization=organization,
            lead=lead,
            context=context,
        )

        profile = compile_org_ai_profile_from_context(context.organization or {})
        requirements = profile.get("qualification", {}).get("requirements", [])
        qualification_state = state_for_lead(lead, requirements=requirements)

        # Conservative zero-LLM extraction: only bind an obvious short reply to
        # the exact requirement the application recorded as last asked.
        if not caller_supplied_context and requirements:
            latest_text = self._latest_inbound_text(context=context)
            latest_id = self._latest_inbound_message_id(context=context)
            direct = apply_unambiguous_reply(
                lead=lead,
                requirements=requirements,
                text=latest_text,
                source_message_id=latest_id,
            )
            qualification_state = direct["state"]
            direct_next = direct.get("next_requirement")
            if (
                not profile.get("communication", {}).get("custom_instructions")
                and not profile.get("communication", {}).get("languages")
                and not (context.pipeline or {}).get("attribute_definitions")
                and
                direct.get("changed")
                and direct.get("answer_status") == REQUIREMENT_ANSWERED
                and qualification_state.get("engagement_mode") == MODE_QUALIFICATION
                and isinstance(direct_next, dict)
                and direct_next.get("can_direct_ask")
                and str(direct_next.get("question") or "").strip()
            ):
                return EngagementDecision(
                    should_engage=True,
                    message=str(direct_next["question"]).strip(),
                    file_document_id=None,
                    crm_actions=[],
                    reason="QUALIFICATION_NEXT",
                    reason_code="QUALIFICATION_NEXT",
                    next_requirement_id=str(direct_next.get("id") or "") or None,
                    model="deterministic",
                )

        # RAG is conditional. Short qualification answers and acknowledgements
        # should not consume an embedding call.
        if not caller_supplied_context:
            query = (knowledge_query or "").strip()
            if not query and self._should_retrieve_knowledge(context=context):
                query = self._build_knowledge_query(context=context)
            if query:
                context = self.context_builder.build(
                    organization=organization,
                    lead=lead,
                    knowledge_query=query,
                    message_limit=self.MESSAGE_LIMIT,
                    knowledge_limit=self.KNOWLEDGE_LIMIT,
                    note_limit=self.NOTE_LIMIT,
                )
                profile = compile_org_ai_profile_from_context(context.organization or {})
                requirements = profile.get("qualification", {}).get("requirements", [])
                qualification_state = state_for_lead(lead, requirements=requirements)

        source_message_id = self._latest_inbound_message_id(context=context)
        claim = None
        result_key = f"shvya:ai:decision:{organization.id}:{lead.id}:{source_message_id}"
        if source_message_id:
            try:
                claim = EngagementGenerationLock(
                    lead_id=getattr(lead, "id", "unknown"),
                    source_message_id=source_message_id,
                )
                if not claim.acquire():
                    saved = cache.get(result_key)
                    if isinstance(saved, dict):
                        return EngagementDecision(**saved)
                    # Another worker is generating, or died before caching its
                    # result. Retry; never turn contention into permanent silence.
                    raise AIProviderTransientError("Engagement generation is already in progress.")
            except EngagementLockError:
                claim = None

        next_item = next_requirement(
            requirements,
            qualification_state.get("requirement_states", {}),
        )
        instructions = self._build_instructions(context=context, profile=profile)
        input_text = self._build_input(
            context=context,
            profile=profile,
            qualification_state=qualification_state,
            next_item=next_item,
        )
        provider = self.provider or OpenAIProvider()
        success = False

        try:
            try:
                result = self._generate_provider_text(
                    provider=provider,
                    instructions=instructions,
                    input_text=input_text,
                    metadata={
                        "organization_id": str(organization.id),
                        "lead_id": str(lead.id),
                        "task": "engagement",
                        "phase": "primary",
                    },
                    response_schema=ENGAGEMENT_RESPONSE_SCHEMA,
                )
            except AIProviderError as exc:
                raise EngagementError("AI engagement generation failed.") from exc

            try:
                decision = self._normalize_result(result=result)
            except EngagementError as first_error:
                decision = self._repair_result_once(
                    provider=provider,
                    organization=organization,
                    lead=lead,
                    result=result,
                    original_error=first_error,
                )

            # Understand free-form answers in the same call as the reply. Only
            # source-backed answers can advance the application-selected question.
            try:
                projected = project_answer_updates(
                    state=qualification_state, requirements=requirements,
                    updates=decision.qualification_updates,
                    messages=(context.conversation or {}).get("messages", []),
                )
            except ValueError as exc:
                raise EngagementError(str(exc)) from exc
            next_item = next_requirement(requirements, projected["requirement_states"])
            # The model cannot choose an arbitrary qualification requirement.
            if decision.next_requirement_id:
                allowed_next_id = str(next_item.get("id")) if next_item else ""
                if decision.next_requirement_id != allowed_next_id:
                    raise EngagementError(
                        "AI selected a qualification requirement other than NEXT_REQUIREMENT."
                    )

            success = True
            if source_message_id:
                try:
                    cache.set(result_key, decision.as_dict(), timeout=180)
                except RedisError:
                    # A cache outage must not discard a valid customer reply.
                    success = False
                    logging.getLogger(__name__).exception("Unable to cache AI decision")
            return decision
        finally:
            if claim is not None:
                try:
                    claim.finish(success=success)
                except EngagementLockError:
                    pass

    def _generate_provider_text(self, *, provider, response_schema=None, **kwargs):
        """Use provider schema support while preserving injected legacy fakes."""
        try:
            return provider.generate_text(response_schema=response_schema, **kwargs)
        except TypeError as exc:
            if "response_schema" not in str(exc):
                raise
            return provider.generate_text(**kwargs)

    def _repair_result_once(
        self,
        *,
        provider,
        organization,
        lead,
        result: AITextResult,
        original_error: EngagementError,
    ) -> EngagementDecision:
        repair_instructions = """
Repair a malformed SHVYA engagement JSON result.
Return ONLY one valid JSON object with exactly these keys:
should_engage, message, file_document_id, crm_actions, qualification_updates, next_requirement_id,
reason_code.
Allowed reason_code values: ANSWER_ORG_QUESTION, QUALIFICATION_NEXT,
QUALIFICATION_CLARIFY, NORMAL_CONVERSATION, HUMAN_HANDOFF, OPT_OUT,
UNKNOWN_INFORMATION, NO_ACTION.
Preserve only the original intended supported response/actions. Do not add facts,
new actions, explanations, markdown, or chain-of-thought.
""".strip()
        repair_input = json.dumps(
            {
                "validation_error": str(original_error),
                "malformed_output": result.text,
            },
            ensure_ascii=False,
        )
        try:
            repaired = self._generate_provider_text(
                provider=provider,
                instructions=repair_instructions,
                input_text=repair_input,
                metadata={
                    "organization_id": str(organization.id),
                    "lead_id": str(lead.id),
                    "task": "engagement",
                    "phase": "schema_repair",
                },
                response_schema=ENGAGEMENT_RESPONSE_SCHEMA,
            )
        except AIProviderError as exc:
            raise EngagementError("AI engagement schema repair failed.") from exc

        return self._normalize_result(result=repaired)

    def _latest_inbound_message_id(self, *, context: AIContext) -> str:
        messages = (context.conversation or {}).get("messages", [])
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("direction") != "inbound":
                continue
            value = str(message.get("id") or "").strip()
            if value:
                return value
        return ""

    def _latest_inbound_text(self, *, context: AIContext) -> str:
        messages = (context.conversation or {}).get("messages", [])
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("direction") != "inbound":
                continue
            body = str(message.get("body") or "").strip()
            if body:
                return body
        return ""

    def _should_retrieve_knowledge(self, *, context: AIContext) -> bool:
        text = self._latest_inbound_text(context=context)
        if not text:
            return False

        normalized = " ".join(text.casefold().split())
        if normalized in self._SIMPLE_ACKS:
            return False

        compact = re.sub(r"[\s,₹$€£+\-./:]", "", normalized)
        if len(normalized) <= 40 and compact and compact.isdigit():
            return False
        if len(normalized) <= 24 and re.fullmatch(
            r"(?:option\s*)?[a-z0-9]{1,8}", normalized
        ):
            return False

        words = set(re.findall(r"[a-z0-9]+", normalized))
        if words & self._KNOWLEDGE_TERMS:
            return True
        if "?" in text and len(normalized) > 20:
            return True
        return False

    def _build_knowledge_query(self, *, context: AIContext) -> str:
        messages = (context.conversation or {}).get("messages", [])
        if not isinstance(messages, list):
            return ""

        recent_messages: list[str] = []
        for message in messages[-4:]:
            if not isinstance(message, dict):
                continue
            body = str(message.get("body") or "").strip()
            if not body:
                continue
            speaker = "Lead" if message.get("direction") == "inbound" else "SHVYA"
            recent_messages.append(f"{speaker}: {body}")

        return "\n".join(recent_messages).strip()[:1800]

    def _build_instructions(self, *, context: AIContext, profile=None) -> str:
        organization_context = context.organization or {}
        profile = profile or compile_org_ai_profile_from_context(organization_context)
        organization_instructions = str(
            profile.get("communication", {}).get("custom_instructions", "") or ""
        ).strip()
        organization_section = organization_instructions or (
            "No additional organization-specific engagement instructions were supplied."
        )
        return (
            f"{SHVYABaseInstructions.get()}\n\n"
            "============================================================\n"
            "ORGANIZATION ENGAGEMENT INSTRUCTIONS\n"
            "============================================================\n"
            f"{organization_section}\n\n"
            "============================================================\n"
            "SHVYA AI ENGAGEMENT TASK\n"
            "============================================================\n"
            f"{self.ENGAGEMENT_TASK_INSTRUCTIONS.strip()}"
        )

    def _recent_conversation_char_budget(self) -> int:
        try:
            configured = int(
                os.getenv(
                    "AI_ENGAGEMENT_CONTEXT_MAX_CHARS",
                    self.DEFAULT_RECENT_CONVERSATION_CHARS,
                )
            )
        except (TypeError, ValueError):
            configured = self.DEFAULT_RECENT_CONVERSATION_CHARS
        return min(max(configured, 2000), 12000)

    def _compact_conversation(self, conversation: dict[str, Any]) -> dict[str, Any]:
        messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
        if not isinstance(messages, list):
            messages = []
        budget = self._recent_conversation_char_budget()
        chosen: list[dict[str, Any]] = []
        used = 0
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            body = str(message.get("body") or "").strip()
            if not body:
                continue
            cost = len(body) + 80
            if chosen and used + cost > budget:
                break
            if not chosen and cost > budget:
                clipped = dict(message)
                clipped["body"] = body[-max(budget - 80, 500):]
                chosen.append(clipped)
                break
            chosen.append(message)
            used += cost
        chosen.reverse()
        return {
            "message_count": len(chosen),
            "messages": chosen,
            "truncated": len(chosen) < len(messages),
        }

    def _build_input(
        self,
        *,
        context: AIContext,
        profile=None,
        qualification_state=None,
        next_item=None,
    ) -> str:
        data = context.as_dict()
        profile = profile or compile_org_ai_profile_from_context(data["organization"] or {})
        lead_data = dict(data["lead"] or {})
        lead_data.pop("attributes", None)
        if qualification_state is not None:
            lead_data["qualification"] = qualification_state

        organization_data = data["organization"] or {}
        organization_payload = {
            "id": organization_data.get("id"),
            "name": organization_data.get("name"),
            "ai_profile": profile,
        }

        payload = {
            "current_time": timezone.now().isoformat(),
            "organization": organization_payload,
            "lead": lead_data,
            "pipeline": data["pipeline"],
            "stage": data["stage"],
            "contacts": data["contacts"],
            "attributes": data["attributes"],
            "conversation_summary": data["conversation_summary"],
            "recent_conversation": self._compact_conversation(data["conversation"] or {}),
            "qualification_notes": data["qualification_notes"],
            "next_requirement": next_item,
            "knowledge": data["knowledge"],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _normalize_result(self, *, result: AITextResult) -> EngagementDecision:
        payload = self._parse_json(result.text)
        schema_version = self._validate_top_level_schema(payload)

        should_engage = payload["should_engage"]
        message = payload["message"]
        file_document_id = payload["file_document_id"]
        crm_actions = payload["crm_actions"]

        if not isinstance(should_engage, bool):
            raise EngagementError("should_engage must be a boolean.")
        if not isinstance(message, str):
            raise EngagementError("message must be a string.")
        message = message.strip()
        if should_engage and not message:
            raise EngagementError(
                "message must not be empty when should_engage is true."
            )
        if not should_engage and message:
            raise EngagementError(
                "message must be empty when should_engage is false."
            )

        if file_document_id is not None and (
            isinstance(file_document_id, bool)
            or not isinstance(file_document_id, int)
            or file_document_id <= 0
        ):
            raise EngagementError("file_document_id must be a positive integer or null.")

        if schema_version in {"v2", "v3"}:
            reason_code = str(payload["reason_code"] or "").strip().upper()
            if reason_code not in REASON_CODES:
                raise EngagementError("reason_code is not allowed.")
            next_requirement_id = payload["next_requirement_id"]
            if next_requirement_id is not None:
                if not isinstance(next_requirement_id, str):
                    raise EngagementError("next_requirement_id must be a string or null.")
                next_requirement_id = next_requirement_id.strip() or None
            reason = reason_code
        else:
            # Backward-compatible parsing for existing tests/playground callers.
            legacy_reason = payload["reason"]
            if not isinstance(legacy_reason, str):
                raise EngagementError("reason must be a string.")
            reason = legacy_reason.strip()[:200]
            reason_code = "NORMAL_CONVERSATION"
            next_requirement_id = None

        try:
            normalized_actions = validate_crm_actions(crm_actions)
        except CRMActionSchemaError as exc:
            raise EngagementError(f"Invalid CRM actions: {exc}") from exc

        return EngagementDecision(
            should_engage=should_engage,
            message=message,
            file_document_id=file_document_id,
            crm_actions=normalized_actions,
            reason=reason,
            reason_code=reason_code,
            next_requirement_id=next_requirement_id,
            model=result.model,
            qualification_updates=payload.get("qualification_updates", []),
        )

    def _parse_json(self, raw_text: str) -> dict[str, Any]:
        text = str(raw_text or "").strip()
        if not text:
            raise EngagementError("AI returned an empty response.")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EngagementError("AI returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise EngagementError("AI response must be a JSON object.")
        return payload

    def _validate_top_level_schema(self, payload: dict[str, Any]) -> str:
        legacy = {
            "should_engage",
            "message",
            "file_document_id",
            "crm_actions",
            "reason",
        }
        current = {
            "should_engage",
            "message",
            "file_document_id",
            "crm_actions",
            "next_requirement_id",
            "reason_code",
        }
        keys = set(payload.keys())
        if keys == current | {"qualification_updates"}:
            return "v3"
        if keys == current:
            return "v2"
        if keys == legacy:
            return "legacy"
        raise EngagementError("AI response contains an invalid schema.")

    def _validate_crm_actions(self, actions: list[Any]) -> None:
        try:
            validate_crm_actions(actions)
        except CRMActionSchemaError as exc:
            raise EngagementError(str(exc)) from exc

    def _validate_context_scope(
        self,
        *,
        organization,
        lead,
        context: AIContext,
    ) -> None:
        organization_id = str((context.organization or {}).get("id") or "")
        lead_id = str((context.lead or {}).get("id") or "")
        if organization_id != str(organization.id):
            raise EngagementError("AI context organization does not match request.")
        if lead_id != str(lead.id):
            raise EngagementError("AI context lead does not match request.")
