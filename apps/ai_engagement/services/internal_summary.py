from __future__ import annotations

from django.db import transaction

from apps.ai_engagement.models import InternalConversationSummary
from apps.crm.models import Lead
from apps.channels.models import WhatsAppMessage


class InternalSummaryError(Exception):
    """
    Raised when an internal conversation summary cannot be
    created or updated.
    """


class InternalSummaryService:
    """
    Builds the internal conversation-summary workflow for a Lead.

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
        - make decisions about bump-up
    """

    DEFAULT_MESSAGE_LIMIT = 100

    MAX_MESSAGE_LIMIT = 500

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

        if lead.organization_id != organization.id:
            raise InternalSummaryError(
                "Lead does not belong to this organization."
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
            .order_by("-created_at", "-id")
            [:limit]
        )

        # Reverse because summary generation should receive the
        # conversation from oldest → newest.
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

        This is the exact text representation that will later be
        provided to the AI provider.
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
        Build the structured input that will eventually be passed
        to the AI Context Builder / provider.
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
                "id": str(
                    lead.pipeline_id
                )
                if lead.pipeline_id
                else None,
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
                "id": str(
                    lead.stage_id
                )
                if lead.stage_id
                else None,
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

        if lead.organization_id != organization.id:
            raise InternalSummaryError(
                "Lead does not belong to this organization."
            )

        return (
            InternalConversationSummary.objects
            .filter(
                organization=organization,
                lead=lead,
                is_active=True,
            )
            .order_by(
                "-generated_at"
            )
            .first()
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
        model_name: str = "",
        generated_by: str = "shvya_ai",
        created_by=None,
    ) -> InternalConversationSummary:
        """
        Publish a completed AI-generated conversation summary.

        The previous active summary is deactivated only inside
        the same transaction in which the new summary is created.
        """

        if lead.organization_id != organization.id:
            raise InternalSummaryError(
                "Lead does not belong to this organization."
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
        # Lock the existing active summary rows for this Lead.
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
        # Unpublish previous summary.
        # --------------------------------------------------------

        InternalConversationSummary.objects.filter(
            organization=organization,
            lead=lead,
            is_active=True,
        ).update(
            is_active=False,
        )

        # --------------------------------------------------------
        # Publish new summary.
        # --------------------------------------------------------

        return (
            InternalConversationSummary.objects.create(
                organization=organization,
                lead=lead,
                summary=summary,
                source_message_count=(
                    source_message_count
                ),
                generated_by=generated_by,
                model_name=model_name,
                is_active=True,
                created_by=created_by,
            )
        )

    # ============================================================
    # PLACEHOLDER GENERATION INTERFACE
    # ============================================================

    def generate_summary(
        self,
        *,
        organization,
        lead: Lead,
        messages: list[WhatsAppMessage],
    ) -> str:
        """
        Generate the internal conversation summary.

        The actual AI provider call will be connected after the
        shared AI Context Builder and provider abstraction are
        implemented.

        This method deliberately does NOT invent an AI summary when
        the provider is unavailable.
        """

        if not messages:
            raise InternalSummaryError(
                "Cannot generate a conversation summary "
                "without conversation messages."
            )

        raise InternalSummaryError(
            "Internal conversation summary generation requires "
            "the configured AI provider."
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
        Prepare all deterministic inputs required by the future
        AI summarization job.

        This method performs no external AI call.

        Returns:
            {
                "messages": [...],
                "conversation_text": "...",
                "summary_input": {...},
            }
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

        return {
            "messages": messages,
            "conversation_text": conversation_text,
            "summary_input": summary_input,
        }