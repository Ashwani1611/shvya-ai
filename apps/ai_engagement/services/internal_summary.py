from __future__ import annotations

from django.db import transaction

from apps.ai_engagement.models import InternalConversationSummary
from apps.ai_engagement.services.ai_provider import (
    AIProviderError,
    OpenAIProvider,
)
from apps.ai_engagement.services.context import (
    AIContextBuilder,
    AIContextError,
)
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead


class InternalSummaryError(Exception):
    """
    Raised when an internal conversation summary cannot be
    created or updated.
    """


class InternalSummaryService:
    """
    Builds and publishes the internal conversation summary
    for a Lead.

    Important separation:

        Internal Conversation Summary
            = summary of the actual conversation

        Qualification Summary
            = qualification-specific result stored in Lead Notes

    This service does NOT:

        - perform lead qualification
        - create qualification notes
        - send messages
        - modify CRM lead fields
        - make bump-up decisions

    AI generation is provider-backed through OpenAIProvider.
    The service itself remains responsible for business rules
    and persistence.
    """

    DEFAULT_MESSAGE_LIMIT = 100

    MAX_MESSAGE_LIMIT = 500

    SUMMARY_INSTRUCTIONS = """
You are SHVYA AI's internal conversation summarizer.

Your task is to summarize the actual conversation between SHVYA
and the lead for internal CRM users.

The summary must be factual and based only on the supplied context.

Include, when supported by the conversation:
- the lead's intent or interests
- important questions asked
- requirements or preferences
- objections, concerns, or blockers
- commitments already made
- agreed or suggested next steps
- unresolved questions or missing information

Do not:
- invent facts
- infer unsupported personal information
- make a qualification decision
- assign a qualification status
- modify CRM data
- recommend an action unless it is clearly grounded in the conversation
- write a customer-facing reply

Write a concise internal CRM summary in clear prose.
"""

    # ============================================================
    # PUBLIC API
    # ============================================================

    def get_messages(
        self,
        *,
        organization,
        lead: Lead,
        limit: int = DEFAULT_MESSAGE_LIMIT,
    ) -> list[WhatsAppMessage]:
        """
        Return recent WhatsApp conversation messages for a Lead.

        Results are organization-scoped and returned in chronological
        order for summary construction.
        """

        self._validate_lead_scope(
            organization=organization,
            lead=lead,
        )

        if limit <= 0:
            raise InternalSummaryError(
                "Message limit must be greater than zero."
            )

        if limit > self.MAX_MESSAGE_LIMIT:
            raise InternalSummaryError(
                f"Message limit cannot exceed "
                f"{self.MAX_MESSAGE_LIMIT}."
            )

        messages = list(
            WhatsAppMessage.objects.filter(
                organization=organization,
                lead=lead,
            )
            .order_by(
                "-created_at",
                "-id",
            )[:limit]
        )

        # Summary generation receives the conversation in
        # chronological order: oldest -> newest.
        messages.reverse()

        return messages

    # ============================================================
    # CONVERSATION SERIALIZATION
    # ============================================================

    def build_conversation_text(
        self,
        messages: list[WhatsAppMessage],
    ) -> str:
        """
        Convert WhatsApp messages into deterministic plain text.

        This is used as the conversation portion of the AI input.
        """

        lines: list[str] = []

        for message in messages:

            body = (
                message.body or ""
            ).strip()

            if not body:
                continue

            if (
                message.direction
                == WhatsAppMessage.Direction.INBOUND
            ):
                speaker = "Lead"
            else:
                speaker = "SHVYA"

            timestamp = (
                message.created_at.isoformat()
                if message.created_at
                else ""
            )

            if timestamp:
                lines.append(
                    f"[{timestamp}] {speaker}: {body}"
                )
            else:
                lines.append(
                    f"{speaker}: {body}"
                )

        return "\n".join(
            lines
        )

    # ============================================================
    # AI INPUT
    # ============================================================

    def build_summary_input(
        self,
        *,
        organization,
        lead: Lead,
        messages: list[WhatsAppMessage],
    ) -> dict:
        """
        Build structured summary input.

        This remains useful for inspection, testing, tracing,
        and future provider adapters.
        """

        return {
            "organization": {
                "id": str(
                    organization.id
                ),
                "name": organization.name,
            },
            "lead": {
                "id": str(
                    lead.id
                ),
                "name": lead.name,
                "phone": lead.phone,
                "email": lead.email,
                "notes": lead.notes,
                "attributes": lead.attributes,
                "lead_source": lead.lead_source,
            },
            "pipeline": {
                "id": (
                    str(
                        lead.pipeline_id
                    )
                    if lead.pipeline_id
                    else None
                ),
                "name": (
                    lead.pipeline.name
                    if lead.pipeline_id
                    else ""
                ),
                "description": (
                    lead.pipeline.description
                    if lead.pipeline_id
                    else ""
                ),
            },
            "stage": {
                "id": (
                    str(
                        lead.stage_id
                    )
                    if lead.stage_id
                    else None
                ),
                "name": (
                    lead.stage.name
                    if lead.stage_id
                    else ""
                ),
                "description": (
                    getattr(
                        lead.stage,
                        "description",
                        "",
                    )
                    if lead.stage_id
                    else ""
                ),
            },
            "conversation": {
                "message_count": len(
                    messages
                ),
                "messages": [
                    {
                        "id": str(
                            message.id
                        ),
                        "direction": message.direction,
                        "body": message.body,
                        "created_at": (
                            message.created_at.isoformat()
                            if message.created_at
                            else None
                        ),
                    }
                    for message in messages
                ],
            },
        }

    # ============================================================
    # CURRENT SUMMARY
    # ============================================================

    def get_current_summary(
        self,
        *,
        organization,
        lead: Lead,
    ) -> InternalConversationSummary | None:
        """
        Return the currently published conversation summary.
        """

        self._validate_lead_scope(
            organization=organization,
            lead=lead,
        )

        return (
            InternalConversationSummary.objects
            .filter(
                organization=organization,
                lead=lead,
                is_active=True,
            )
            .order_by(
                "-generated_at",
                "-id",
            )
            .first()
        )

    # ============================================================
    # SUMMARY FRESHNESS
    # ============================================================

    def is_summary_stale(
        self,
        *,
        organization,
        lead: Lead,
        latest_message: WhatsAppMessage | None = None,
    ) -> bool:
        """
        Determine whether the currently published summary no
        longer represents the latest conversation state.

        No summary means stale.

        If a latest message exists, its ID and timestamp are
        compared with the summary's source watermark.
        """

        self._validate_lead_scope(
            organization=organization,
            lead=lead,
        )

        summary = self.get_current_summary(
            organization=organization,
            lead=lead,
        )

        if summary is None:
            return True

        if latest_message is None:
            latest_message = (
                WhatsAppMessage.objects
                .filter(
                    organization=organization,
                    lead=lead,
                )
                .order_by(
                    "-created_at",
                    "-id",
                )
                .first()
            )

        # No messages means there is nothing new to summarize.
        if latest_message is None:
            return False

        if (
            summary.source_last_message_id
            != latest_message.id
        ):
            return True

        if (
            summary.source_last_message_at
            != latest_message.created_at
        ):
            return True

        return False

    # ============================================================
    # AI CONTEXT
    # ============================================================

    def build_ai_context(
        self,
        *,
        organization,
        lead: Lead,
        messages: list[WhatsAppMessage],
    ):
        """
        Build the centralized SHVYA AI context.

        Knowledge retrieval is intentionally not forced here until
        a real query embedding is available.

        Internal conversation summary is already represented by
        this service and therefore is not required as an input
        dependency for generating the new summary itself.
        """

        try:
            return AIContextBuilder().build(
                organization=organization,
                lead=lead,
                query_vector=None,
                message_limit=max(
                    len(messages),
                    1,
                ),
                knowledge_limit=5,
            )
        except AIContextError as exc:
            raise InternalSummaryError(
                f"Unable to build AI context: {exc}"
            ) from exc

    # ============================================================
    # AI PROMPT INPUT
    # ============================================================

    def build_provider_input(
        self,
        *,
        organization,
        lead: Lead,
        messages: list[WhatsAppMessage],
    ) -> str:
        """
        Convert the relevant context into a bounded provider input.

        The conversation itself is the primary source for this
        summary. CRM metadata is supplied only as context.
        """

        context = self.build_ai_context(
            organization=organization,
            lead=lead,
            messages=messages,
        )

        context_data = context.as_dict()

        organization_data = (
            context_data["organization"]
        )

        lead_data = (
            context_data["lead"]
        )

        pipeline_data = (
            context_data["pipeline"]
        )

        stage_data = (
            context_data["stage"]
        )

        contacts_data = (
            context_data["contacts"]
        )

        attributes_data = (
            context_data["attributes"]
        )

        conversation_data = (
            context_data["conversation"]
        )

        qualification_notes = (
            context_data["qualification_notes"]
        )

        lines = [
            "SHVYA INTERNAL CONVERSATION SUMMARY INPUT",
            "",
            "ORGANIZATION",
            f"Name: {organization_data.get('name', '')}",
            f"About: {organization_data.get('about', '')}",
            "",
            "LEAD",
            f"Name: {lead_data.get('name', '')}",
            f"Lead source: {lead_data.get('lead_source', '')}",
            "",
            "PIPELINE",
            f"Name: {pipeline_data.get('name', '')}",
            f"Description: {pipeline_data.get('description', '')}",
            "",
            "STAGE",
            f"Name: {stage_data.get('name', '')}",
            f"Description: {stage_data.get('description', '')}",
            "",
            "CONTACTS",
            str(contacts_data),
            "",
            "ATTRIBUTES",
            str(attributes_data),
            "",
            "EXISTING QUALIFICATION NOTES",
            str(qualification_notes),
            "",
            "CONVERSATION",
        ]

        for message in conversation_data["messages"]:
            timestamp = (
                message.get("created_at")
                or ""
            )

            speaker = (
                "Lead"
                if message.get("speaker") == "lead"
                else "SHVYA"
            )

            body = (
                message.get("body")
                or ""
            ).strip()

            if not body:
                continue

            lines.append(
                f"[{timestamp}] {speaker}: {body}"
            )

        return "\n".join(
            lines
        ).strip()

    # ============================================================
    # AI GENERATION
    # ============================================================

    def generate_summary(
        self,
        *,
        organization,
        lead: Lead,
        messages: list[WhatsAppMessage],
    ) -> tuple[str, str]:
        """
        Generate the internal conversation summary through the
        configured AI provider.

        Returns:
            (summary_text, model_name)
        """

        if not messages:
            raise InternalSummaryError(
                "Cannot generate a conversation summary "
                "without conversation messages."
            )

        provider_input = (
            self.build_provider_input(
                organization=organization,
                lead=lead,
                messages=messages,
            )
        )

        if not provider_input:
            raise InternalSummaryError(
                "Conversation summary input is empty."
            )

        try:
            provider = OpenAIProvider()

            result = provider.generate_text(
                instructions=self.SUMMARY_INSTRUCTIONS,
                input_text=provider_input,
                metadata={
                    "organization_id": str(
                        organization.id
                    ),
                    "lead_id": str(
                        lead.id
                    ),
                    "purpose": "internal_conversation_summary",
                },
            )

        except AIProviderError as exc:

            raise InternalSummaryError(
                f"AI summary generation failed: {exc}"
            ) from exc

        summary = (
            result.text or ""
        ).strip()

        if not summary:
            raise InternalSummaryError(
                "AI provider returned an empty conversation summary."
            )

        return (
            summary,
            result.model,
        )

    # ============================================================
    # PUBLISH SUMMARY
    # ============================================================

    @transaction.atomic
    def publish_summary(
        self,
        *,
        organization,
        lead: Lead,
        summary: str,
        source_message_count: int,
        source_last_message_id=None,
        source_last_message_at=None,
        model_name: str = "",
        generated_by: str = "shvya_ai",
        created_by=None,
    ) -> InternalConversationSummary:
        """
        Publish a completed internal conversation summary.

        Publication is transactional:

            previous active summary
                ↓
            inactive

            new completed summary
                ↓
            active

        Source watermark identifies exactly which conversation
        state produced the summary.
        """

        self._validate_lead_scope(
            organization=organization,
            lead=lead,
        )

        summary = (
            summary or ""
        ).strip()

        if not summary:
            raise InternalSummaryError(
                "Summary cannot be empty."
            )

        if source_message_count < 0:
            raise InternalSummaryError(
                "source_message_count cannot be negative."
            )

        # --------------------------------------------------------
        # Lock currently active summary rows.
        # --------------------------------------------------------

        list(
            InternalConversationSummary.objects
            .select_for_update()
            .filter(
                organization=organization,
                lead=lead,
                is_active=True,
            )
        )

        # --------------------------------------------------------
        # Ensure only one active summary remains.
        # --------------------------------------------------------

        InternalConversationSummary.objects.filter(
            organization=organization,
            lead=lead,
            is_active=True,
        ).update(
            is_active=False,
        )

        # --------------------------------------------------------
        # Publish the new summary.
        # --------------------------------------------------------

        return (
            InternalConversationSummary.objects.create(
                organization=organization,
                lead=lead,
                summary=summary,
                source_message_count=(
                    source_message_count
                ),
                source_last_message_id=(
                    source_last_message_id
                ),
                source_last_message_at=(
                    source_last_message_at
                ),
                generated_by=generated_by,
                model_name=model_name,
                is_active=True,
                created_by=created_by,
            )
        )

    # ============================================================
    # COMPLETE WORKFLOW
    # ============================================================

    def prepare_summary(
        self,
        *,
        organization,
        lead: Lead,
        limit: int = DEFAULT_MESSAGE_LIMIT,
    ) -> dict:
        """
        Prepare deterministic inputs required for AI summary
        generation.

        No external AI call is made here.
        """

        messages = self.get_messages(
            organization=organization,
            lead=lead,
            limit=limit,
        )

        conversation_text = (
            self.build_conversation_text(
                messages
            )
        )

        summary_input = (
            self.build_summary_input(
                organization=organization,
                lead=lead,
                messages=messages,
            )
        )

        latest_message = (
            messages[-1]
            if messages
            else None
        )

        return {
            "messages": messages,
            "conversation_text": conversation_text,
            "summary_input": summary_input,
            "source_message_count": len(
                messages
            ),
            "source_last_message_id": (
                latest_message.id
                if latest_message
                else None
            ),
            "source_last_message_at": (
                latest_message.created_at
                if latest_message
                else None
            ),
        }

    # ============================================================
    # FULL GENERATE + PUBLISH WORKFLOW
    # ============================================================

    def generate_and_publish(
        self,
        *,
        organization,
        lead: Lead,
        limit: int = DEFAULT_MESSAGE_LIMIT,
        created_by=None,
    ) -> InternalConversationSummary:
        """
        Generate and publish the current conversation summary.

        This method is designed to be called by the Celery task,
        not by the Lead Card request.
        """

        prepared = self.prepare_summary(
            organization=organization,
            lead=lead,
            limit=limit,
        )

        messages = prepared["messages"]

        if not messages:
            raise InternalSummaryError(
                "Cannot generate a conversation summary "
                "because this lead has no WhatsApp messages."
            )

        summary, model_name = (
            self.generate_summary(
                organization=organization,
                lead=lead,
                messages=messages,
            )
        )

        return self.publish_summary(
            organization=organization,
            lead=lead,
            summary=summary,
            source_message_count=(
                prepared["source_message_count"]
            ),
            source_last_message_id=(
                prepared["source_last_message_id"]
            ),
            source_last_message_at=(
                prepared["source_last_message_at"]
            ),
            model_name=model_name,
            generated_by="shvya_ai",
            created_by=created_by,
        )

    # ============================================================
    # VALIDATION
    # ============================================================

    def _validate_lead_scope(
        self,
        *,
        organization,
        lead: Lead,
    ) -> None:
        """
        Enforce organization ownership.
        """

        if organization is None:
            raise InternalSummaryError(
                "Organization is required."
            )

        if lead is None:
            raise InternalSummaryError(
                "Lead is required."
            )

        if lead.organization_id != organization.id:
            raise InternalSummaryError(
                "Lead does not belong to this organization."
            )