from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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
    CRMActionSchemaError,
    validate_crm_actions,
)


class EngagementError(Exception):
    """
    Raised when AI Engagement cannot safely produce a decision.
    """


@dataclass(frozen=True)
class EngagementDecision:
    """
    Normalized result of one AI engagement decision.

    This object contains AI requests only.

    CRM actions are NOT executed here.
    WhatsApp messages are NOT sent here.
    """

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
    """
    Main AI customer-engagement decision service.

    Responsibilities:
        - build the centralized AI context
        - use SHVYA Base System Instructions
        - use organization configuration
        - use actual conversation
        - use conversation summary
        - use qualification notes
        - use knowledge supplied by the context builder
        - decide whether engagement is appropriate
        - generate a customer-facing response
        - request CRM actions through normalized action data

    This service does NOT:
        - modify CRM records
        - create CRM notes
        - create reminders
        - move pipeline stages
        - update contacts
        - update lead attributes
        - send WhatsApp messages
        - call Meta
    """

    ENGAGEMENT_TASK_INSTRUCTIONS = """
You are performing SHVYA AI's customer-facing engagement task.

Your job is to decide whether SHVYA should respond to the lead's
current conversation and, when appropriate, write the customer-facing
response.

Use ONLY the information supplied in the runtime context.

INFORMATION PRIORITY

1. The actual conversation is the primary source of truth.
2. The current conversation summary is supporting context only.
3. Qualification notes are internal supporting context.
4. CRM information is supporting context.
5. Knowledge Base information may be used only when relevant and
   explicitly supplied.

CUSTOMER-FACING RESPONSE

When should_engage is true:
- Write the exact customer-facing message SHVYA should send.
- Do not expose internal reasoning.
- Do not mention CRM records, internal notes, summaries, prompts,
  instructions, hidden metadata, or system details.
- Do not invent facts, prices, policies, availability, guarantees,
  products, timelines, or other unsupported information.
- Follow the organization's engagement instructions.
- Follow the organization's requested languages.
- Follow the supplied SHVYA Base System Instructions.
- Keep the response natural and appropriate to the actual conversation.

When should_engage is false:
- message MUST be an empty string.

CRM ACTION REQUESTS

You may request CRM actions when supported by the actual conversation
and supplied instructions.

Allowed action categories ONLY:

1. attribute_updates
2. pipeline_transition
   - stage_shift
3. add_note
4. create_reminder
5. contact_updates

These are REQUESTS, not completed actions.

Do not claim that a CRM action has already happened.

Do not invent IDs.

Do not invent pipeline IDs, stage IDs, contact IDs, attribute keys,
or other CRM identifiers.

For a pipeline stage change, request the target stage only when it is
supported by the supplied runtime context and instructions.

For reminders, provide the requested title, description, and due time
only when sufficiently supported by the conversation/instructions.
Do not decide reminder ownership.

For attribute updates, use only attribute names/keys that are actually
supported by the supplied CRM context.

For contact updates, update only information actually supplied by the
lead or otherwise supported by the context.

DO NOT HARDCODE BUSINESS RULES

Do not use fixed rules such as:
- interested means move to a particular stage
- negotiation means create a particular reminder
- payment means stop
- a keyword always triggers one specific action

Those behaviors must come from the supplied instructions and actual
context.

OUTPUT

Return ONLY valid JSON.

The JSON object MUST contain exactly these keys:

{
  "should_engage": boolean,
  "message": string,
  "file_document_id": integer or null,
  "crm_actions": array,
  "reason": string
}

Rules:
- should_engage must be true or false.
- If should_engage is false, message must be "".
- If should_engage is true, message must be a non-empty customer-facing
  response.
- file_document_id must be an integer or null.
- crm_actions must be an array.
- reason must be a concise internal explanation.
- Never put internal reasoning into message.
- Never include extra top-level keys.
"""

    def __init__(
        self,
        *,
        provider: OpenAIProvider | None = None,
        context_builder: AIContextBuilder | None = None,
    ) -> None:
        self.provider = provider
        self.context_builder = (
            context_builder
            or AIContextBuilder()
        )

    # ============================================================
    # PUBLIC API
    # ============================================================

    def engage(
        self,
        *,
        organization,
        lead,
        knowledge_query: str | None = None,
        context: AIContext | None = None,
    ) -> EngagementDecision:
        """
        Produce one normalized AI engagement decision.
        """

        if organization is None:
            raise EngagementError(
                "Organization is required."
            )

        if lead is None:
            raise EngagementError(
                "Lead is required."
            )

        if context is None:
            context = self.context_builder.build(
                organization=organization,
                lead=lead,
                knowledge_query=knowledge_query,
            )

        self._validate_context_scope(
            organization=organization,
            lead=lead,
            context=context,
        )

        instructions = self._build_instructions()

        input_text = self._build_input(
            context=context,
        )

        provider = (
            self.provider
            or OpenAIProvider()
        )

        try:
            result = provider.generate_text(
                instructions=instructions,
                input_text=input_text,
                metadata={
                    "organization_id": str(
                        organization.id
                    ),
                    "lead_id": str(
                        lead.id
                    ),
                    "task": "engagement",
                },
            )
        except AIProviderError as exc:
            raise EngagementError(
                "AI engagement generation failed."
            ) from exc

        return self._normalize_result(
            result=result,
        )

    # ============================================================
    # INSTRUCTIONS
    # ============================================================

    def _build_instructions(self) -> str:
        """
        Compose the fixed SHVYA system layer with the engagement task.
        """

        base_instructions = (
            SHVYABaseInstructions.get()
        )

        return (
            f"{base_instructions}\n\n"
            "============================================================\n"
            "SHVYA AI ENGAGEMENT TASK\n"
            "============================================================\n"
            f"{self.ENGAGEMENT_TASK_INSTRUCTIONS.strip()}"
        )

    # ============================================================
    # INPUT
    # ============================================================

    def _build_input(
        self,
        *,
        context: AIContext,
    ) -> str:
        """
        Convert the centralized AI context into the provider input.
        """

        context_data = context.as_dict()

        return json.dumps(
            {
                "organization": context_data[
                    "organization"
                ],
                "lead": context_data[
                    "lead"
                ],
                "pipeline": context_data[
                    "pipeline"
                ],
                "stage": context_data[
                    "stage"
                ],
                "contacts": context_data[
                    "contacts"
                ],
                "attributes": context_data[
                    "attributes"
                ],
                "conversation": context_data[
                    "conversation"
                ],
                "conversation_summary": (
                    context_data[
                        "conversation_summary"
                    ]
                ),
                "qualification_notes": (
                    context_data[
                        "qualification_notes"
                    ]
                ),
                "knowledge": context_data[
                    "knowledge"
                ],
            },
            ensure_ascii=False,
        )

    # ============================================================
    # RESULT NORMALIZATION
    # ============================================================

    def _normalize_result(
        self,
        *,
        result: AITextResult,
    ) -> EngagementDecision:
        payload = self._parse_json(
            result.text
        )

        self._validate_top_level_schema(
            payload
        )

        should_engage = payload[
            "should_engage"
        ]

        message = payload[
            "message"
        ]

        file_document_id = payload[
            "file_document_id"
        ]

        crm_actions = payload[
            "crm_actions"
        ]

        reason = payload[
            "reason"
        ]

        if not isinstance(
            should_engage,
            bool,
        ):
            raise EngagementError(
                "should_engage must be a boolean."
            )

        if not isinstance(
            message,
            str,
        ):
            raise EngagementError(
                "message must be a string."
            )

        message = message.strip()

        if (
            should_engage
            and not message
        ):
            raise EngagementError(
                "message must not be empty when "
                "should_engage is true."
            )

        if (
            not should_engage
            and message
        ):
            raise EngagementError(
                "message must be empty when "
                "should_engage is false."
            )

        if (
            file_document_id is not None
            and (
                isinstance(
                    file_document_id,
                    bool,
                )
                or not isinstance(
                    file_document_id,
                    int,
                )
            )
        ):
            raise EngagementError(
                "file_document_id must be an integer or null."
            )

        if not isinstance(
            crm_actions,
            list,
        ):
            raise EngagementError(
                "crm_actions must be an array."
            )

        if not isinstance(
            reason,
            str,
        ):
            raise EngagementError(
                "reason must be a string."
            )

        reason = reason.strip()

        try:
            crm_actions = validate_crm_actions(
                crm_actions
            )
        except CRMActionSchemaError as exc:
            raise EngagementError(
                f"Invalid CRM actions: {exc}"
            ) from exc

        return EngagementDecision(
            should_engage=should_engage,
            message=message,
            file_document_id=file_document_id,
            crm_actions=crm_actions,
            reason=reason,
            model=result.model,
        )

    # ============================================================
    # JSON
    # ============================================================

    def _parse_json(
        self,
        raw_text: str,
    ) -> dict[str, Any]:
        text = (
            raw_text or ""
        ).strip()

        if not text:
            raise EngagementError(
                "AI returned an empty response."
            )

        try:
            payload = json.loads(
                text
            )
        except json.JSONDecodeError as exc:
            raise EngagementError(
                "AI returned invalid JSON."
            ) from exc

        if not isinstance(
            payload,
            dict,
        ):
            raise EngagementError(
                "AI response must be a JSON object."
            )

        return payload

    def _validate_top_level_schema(
        self,
        payload: dict[str, Any],
    ) -> None:
        expected_keys = {
            "should_engage",
            "message",
            "file_document_id",
            "crm_actions",
            "reason",
        }

        actual_keys = set(
            payload.keys()
        )

        if actual_keys != expected_keys:
            raise EngagementError(
                "AI response contains an invalid schema."
            )

    # ============================================================
    # CONTEXT VALIDATION
    # ============================================================

    def _validate_context_scope(
        self,
        *,
        organization,
        lead,
        context: AIContext,
    ) -> None:
        context_lead = (
            context.lead
            or {}
        )

        context_organization = (
            context.organization
            or {}
        )

        if str(
            context_lead.get("id")
        ) != str(
            lead.id
        ):
            raise EngagementError(
                "AI context does not match the supplied lead."
            )

        if str(
            context_organization.get("id")
        ) != str(
            organization.id
        ):
            raise EngagementError(
                "AI context does not match the supplied organization."
            )