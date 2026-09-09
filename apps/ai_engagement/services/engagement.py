from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from apps.ai_engagement.prompts.engagement import (
    CUSTOMER_ENGAGEMENT_INSTRUCTIONS,
)
from apps.ai_engagement.services.ai_provider import (
    AIProviderError,
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


class EngagementError(Exception):
    """Raised when AI Engagement cannot safely produce a decision."""


@dataclass(frozen=True)
class EngagementDecision:
    """Normalized AI proposal. Side effects are executed elsewhere."""

    should_engage: bool
    message: str
    file_document_id: int | None
    crm_actions: list[dict[str, Any]]
    reason: str
    model: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "should_engage": self.should_engage,
            "message": self.message,
            "file_document_id": self.file_document_id,
            "crm_actions": self.crm_actions,
            "reason": self.reason,
            "model": self.model,
        }


class EngagementService:
    """One-call customer engagement orchestration.

    Python owns permissions, persisted qualification state, CRM validation,
    stage transitions, idempotency and sending. The model receives a compact
    context and performs only the language work that benefits from an LLM:
    understand the turn, notice supported lead facts, choose the next
    conversational step, generate the reply, and propose allowed CRM actions.
    """

    ENGAGEMENT_TASK_INSTRUCTIONS = CUSTOMER_ENGAGEMENT_INSTRUCTIONS
    ALLOWED_ACTION_TYPES = ALLOWED_ACTION_TYPES
    ALLOWED_PIPELINE_TRANSITION_TYPES = {"stage_shift"}

    MESSAGE_LIMIT = 12
    KNOWLEDGE_LIMIT = 3
    NOTE_LIMIT = 5

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

        # RAG is conditional. Short qualification answers and acknowledgements
        # should not consume an embedding call. Explicit knowledge_query always
        # wins when a caller deliberately asks for retrieval.
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

        source_message_id = self._latest_inbound_message_id(context=context)
        claim = None
        if source_message_id:
            try:
                claim = EngagementGenerationLock(
                    lead_id=lead.id,
                    source_message_id=source_message_id,
                )
                if not claim.acquire():
                    return EngagementDecision(
                        should_engage=False,
                        message="",
                        file_document_id=None,
                        crm_actions=[],
                        reason="duplicate_generation_in_progress",
                        model="",
                    )
            except EngagementLockError:
                # Redis lock failure must not make customer engagement fail.
                # Existing DB-level duplicate/freshness checks still fail safe.
                claim = None

        instructions = self._build_instructions(context=context)
        input_text = self._build_input(context=context)
        provider = self.provider or OpenAIProvider()
        success = False

        try:
            try:
                result = provider.generate_text(
                    instructions=instructions,
                    input_text=input_text,
                    metadata={
                        "organization_id": str(organization.id),
                        "lead_id": str(lead.id),
                        "task": "engagement",
                        "phase": "primary",
                    },
                )
            except AIProviderError as exc:
                raise EngagementError("AI engagement generation failed.") from exc

            try:
                decision = self._normalize_result(result=result)
            except EngagementError as first_error:
                # One bounded schema-repair call is cheaper and safer than
                # blindly retrying the full customer request three times.
                decision = self._repair_result_once(
                    provider=provider,
                    organization=organization,
                    lead=lead,
                    result=result,
                    original_error=first_error,
                )

            success = True
            return decision
        finally:
            if claim is not None:
                try:
                    claim.finish(success=success)
                except EngagementLockError:
                    # Do not hide a successfully generated customer decision or
                    # replace the original provider/validation error.
                    pass

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
should_engage, message, file_document_id, crm_actions, reason.
Preserve the original intended customer response and supported actions. Do not
add facts, new actions, explanations, markdown, or chain-of-thought.
If should_engage is false, message must be empty.
""".strip()
        repair_input = json.dumps(
            {
                "validation_error": str(original_error),
                "malformed_output": result.text,
            },
            ensure_ascii=False,
        )
        try:
            repaired = provider.generate_text(
                instructions=repair_instructions,
                input_text=repair_input,
                metadata={
                    "organization_id": str(organization.id),
                    "lead_id": str(lead.id),
                    "task": "engagement",
                    "phase": "schema_repair",
                },
            )
        except AIProviderError as exc:
            raise EngagementError("AI engagement schema repair failed.") from exc

        # No recursive repair. One malformed repair is a hard validation error.
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

        # Numbers, dates, phone-like values and short option answers are common
        # qualification replies and do not need RAG.
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

        # A genuine question may require organization knowledge. Keep very
        # short scheduling/confirmation turns out of RAG where possible.
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
            speaker = (
                "Lead" if message.get("direction") == "inbound" else "SHVYA"
            )
            recent_messages.append(f"{speaker}: {body}")

        return "\n".join(recent_messages).strip()[:2500]

    def _build_instructions(self, *, context: AIContext) -> str:
        organization_context = context.organization or {}
        organization_instructions = str(
            organization_context.get("engagement_instructions", "") or ""
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

    def _build_input(self, *, context: AIContext) -> str:
        data = context.as_dict()
        lead_data = dict(data["lead"] or {})
        # The normalized `attributes` list below is the source supplied to the
        # model. Avoid sending the same Lead.attributes JSON twice.
        lead_data.pop("attributes", None)

        payload = {
            "current_time": timezone.now().isoformat(),
            "organization": data["organization"],
            "lead": lead_data,
            "pipeline": data["pipeline"],
            "stage": data["stage"],
            "contacts": data["contacts"],
            "attributes": data["attributes"],
            "conversation_summary": data["conversation_summary"],
            "recent_conversation": data["conversation"],
            "qualification_notes": data["qualification_notes"],
            "knowledge": data["knowledge"],
        }
        return json.dumps(payload, ensure_ascii=False)

    def _normalize_result(self, *, result: AITextResult) -> EngagementDecision:
        payload = self._parse_json(result.text)
        self._validate_top_level_schema(payload)

        should_engage = payload["should_engage"]
        message = payload["message"]
        file_document_id = payload["file_document_id"]
        crm_actions = payload["crm_actions"]
        reason = payload["reason"]

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

        if not isinstance(reason, str):
            raise EngagementError("reason must be a string.")
        reason = reason.strip()[:500]

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
            model=result.model,
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

    def _validate_top_level_schema(self, payload: dict[str, Any]) -> None:
        expected = {
            "should_engage",
            "message",
            "file_document_id",
            "crm_actions",
            "reason",
        }
        if set(payload.keys()) != expected:
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
