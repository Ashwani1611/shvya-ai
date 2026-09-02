from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.ai_engagement.models import (
    InternalConversationSummary,
    OrgInfo,
)
from apps.ai_engagement.services.embeddings import (
    EmbeddingError,
    EmbeddingService,
)
from apps.ai_engagement.services.retrieval import (
    KnowledgeRetrievalService,
)
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead


class AIContextError(Exception):
    """
    Raised when AI context cannot be built safely.
    """


@dataclass(frozen=True)
class AIContext:
    """
    Immutable runtime context used by SHVYA AI services.

    This is a runtime object only.
    It is not persisted as another source-of-truth record.
    """

    organization: dict[str, Any]
    lead: dict[str, Any]
    pipeline: dict[str, Any]
    stage: dict[str, Any]
    contacts: list[dict[str, Any]]
    attributes: list[dict[str, Any]]
    conversation: dict[str, Any]

    # ------------------------------------------------------------
    # Conversation Summary
    #
    # This is the internal chat/conversation summary.
    #
    # UI:
    # Lead Card
    #     ↓
    # Conversation Summary icon
    #     ↓
    # Read-only summary
    # ------------------------------------------------------------

    conversation_summary: dict[str, Any] | None

    # ------------------------------------------------------------
    # Qualification Notes
    #
    # Qualification Summary is stored through the existing
    # LeadNote model and therefore remains separate from
    # conversation_summary.
    #
    # UI:
    # Lead Card
    #     ↓
    # Notes section
    # ------------------------------------------------------------

    qualification_notes: list[dict[str, Any]]

    knowledge: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        """
        Return the runtime AI context as a normal dictionary.
        """

        return {
            "organization": self.organization,
            "lead": self.lead,
            "pipeline": self.pipeline,
            "stage": self.stage,
            "contacts": self.contacts,
            "attributes": self.attributes,
            "conversation": self.conversation,
            "conversation_summary": self.conversation_summary,
            "qualification_notes": self.qualification_notes,
            "knowledge": self.knowledge,
        }


class AIContextBuilder:
    """
    Centralized SHVYA AI context builder.

    Existing sources of truth:

        Organization
        OrgInfo
        Lead
        Pipeline
        Stage
        LeadContact
        LeadNote
        LeadActivity
        WhatsAppMessage
        InternalConversationSummary
        Knowledge Base

    Responsibilities:

        - enforce organization scope
        - read organization AI configuration
        - read CRM lead state
        - read pipeline/stage state
        - read lead contacts
        - read lead attributes
        - read recent WhatsApp messages
        - read current internal conversation summary
        - read existing Lead Notes
        - retrieve relevant Knowledge Base context
        - assemble one consistent runtime context

    This service does NOT:

        - generate AI output
        - generate embeddings
        - modify CRM records
        - modify Lead Notes
        - modify Conversation Summary
        - perform qualification
        - send messages
        - perform bump-up decisions
    """

    DEFAULT_MESSAGE_LIMIT = 50
    MAX_MESSAGE_LIMIT = 500

    DEFAULT_KNOWLEDGE_LIMIT = 5
    MAX_KNOWLEDGE_LIMIT = 20

    DEFAULT_NOTE_LIMIT = 20
    MAX_NOTE_LIMIT = 100

    # ============================================================
    # PUBLIC BUILD
    # ============================================================

    def build(
        self,
        *,
        organization,
        lead: Lead,
        knowledge_query: str | None = None,
        query_vector: list[float] | None = None,
        message_limit: int = DEFAULT_MESSAGE_LIMIT,
        knowledge_limit: int = DEFAULT_KNOWLEDGE_LIMIT,
        note_limit: int = DEFAULT_NOTE_LIMIT,
    ) -> AIContext:
        """
        Build the complete runtime AI context for one Lead.

        Knowledge retrieval accepts either a pre-computed
        query_vector, or a raw knowledge_query string that is
        embedded internally via EmbeddingService.
        """

        self._validate_lead_scope(
            organization=organization,
            lead=lead,
        )

        message_limit = (
            self._validate_message_limit(
                message_limit
            )
        )

        knowledge_limit = (
            self._validate_knowledge_limit(
                knowledge_limit
            )
        )

        note_limit = (
            self._validate_note_limit(
                note_limit
            )
        )

        organization_context = (
            self._build_organization_context(
                organization=organization,
            )
        )

        lead_context = (
            self._build_lead_context(
                lead=lead,
            )
        )

        pipeline_context = (
            self._build_pipeline_context(
                lead=lead,
            )
        )

        stage_context = (
            self._build_stage_context(
                lead=lead,
            )
        )

        contacts = (
            self._build_contacts(
                lead=lead,
            )
        )

        attributes = (
            self._build_attributes(
                lead=lead,
            )
        )

        conversation_messages = (
            self._get_messages(
                organization=organization,
                lead=lead,
                limit=message_limit,
            )
        )

        conversation = (
            self._build_conversation_context(
                messages=conversation_messages,
            )
        )

        conversation_summary = (
            self._build_conversation_summary(
                organization=organization,
                lead=lead,
            )
        )

        qualification_notes = (
            self._build_qualification_notes(
                lead=lead,
                limit=note_limit,
            )
        )

        knowledge = (
            self._build_knowledge_context(
                organization=organization,
                knowledge_query=knowledge_query,
                query_vector=query_vector,
                limit=knowledge_limit,
            )
        )

        return AIContext(
            organization=organization_context,
            lead=lead_context,
            pipeline=pipeline_context,
            stage=stage_context,
            contacts=contacts,
            attributes=attributes,
            conversation=conversation,
            conversation_summary=conversation_summary,
            qualification_notes=qualification_notes,
            knowledge=knowledge,
        )

    # ============================================================
    # ORGANIZATION + ORG INFO
    # ============================================================

    def _build_organization_context(
        self,
        *,
        organization,
    ) -> dict[str, Any]:
        """
        Build Organization + OrgInfo context.
        """

        org_info = (
            OrgInfo.objects
            .filter(
                organization=organization,
            )
            .first()
        )

        if org_info is None:
            return {
                "id": str(
                    organization.id
                ),
                "name": organization.name,
                "ai_enabled": False,
                "about": "",
                "bot_languages": "",
                "qualification_requirements": "",
                "bump_up_enabled": False,
                "bump_up_count": 0,
            }

        return {
            "id": str(
                organization.id
            ),
            "name": organization.name,
            "ai_enabled": org_info.ai_enabled,
            "about": org_info.about,
            "bot_languages": org_info.bot_languages,
            "qualification_requirements": (
                org_info.qualification_requirements
            ),
            "bump_up_enabled": (
                org_info.bump_up_enabled
            ),
            "bump_up_count": (
                org_info.bump_up_count
            ),
        }

    # ============================================================
    # LEAD
    # ============================================================

    def _build_lead_context(
        self,
        *,
        lead: Lead,
    ) -> dict[str, Any]:
        """
        Build normalized Lead context.
        """

        return {
            "id": str(
                lead.id
            ),
            "name": lead.name,
            "phone": lead.phone,
            "email": lead.email,
            "notes": lead.notes,
            "attributes": lead.attributes,
            "lead_source": lead.lead_source,
            "stage_entered_at": (
                lead.stage_entered_at.isoformat()
                if lead.stage_entered_at
                else None
            ),
            "created_at": (
                lead.created_at.isoformat()
                if lead.created_at
                else None
            ),
            "updated_at": (
                lead.updated_at.isoformat()
                if lead.updated_at
                else None
            ),
        }

    # ============================================================
    # PIPELINE
    # ============================================================

    def _build_pipeline_context(
        self,
        *,
        lead: Lead,
    ) -> dict[str, Any]:
        """
        Build Pipeline context.
        """

        pipeline = lead.pipeline

        if pipeline is None:
            return {}

        return {
            "id": str(
                pipeline.id
            ),
            "name": pipeline.name,
            "description": pipeline.description,
            "country_code": pipeline.country_code,
            "phone_number": pipeline.phone_number,
            "is_active": pipeline.is_active,
        }

    # ============================================================
    # STAGE
    # ============================================================

    def _build_stage_context(
        self,
        *,
        lead: Lead,
    ) -> dict[str, Any]:
        """
        Build Stage context including its description.
        """

        stage = lead.stage

        if stage is None:
            return {}

        return {
            "id": str(
                stage.id
            ),
            "name": stage.name,
            "description": stage.description,
            "display_order": stage.display_order,
            "color": stage.color,
            "is_active": stage.is_active,
            "config": stage.config,
        }

    # ============================================================
    # CONTACTS
    # ============================================================

    def _build_contacts(
        self,
        *,
        lead: Lead,
    ) -> list[dict[str, Any]]:
        """
        Build LeadContact context.
        """

        contacts = (
            lead.contacts
            .order_by(
                "-created_at",
            )
        )

        return [
            {
                "id": str(
                    contact.id
                ),
                "channel": contact.channel,
                "handle": contact.handle,
                "verified": contact.verified,
                "metadata": contact.metadata,
                "created_at": (
                    contact.created_at.isoformat()
                    if contact.created_at
                    else None
                ),
            }
            for contact in contacts
        ]

    # ============================================================
    # ATTRIBUTES
    # ============================================================

    def _build_attributes(
        self,
        *,
        lead: Lead,
    ) -> list[dict[str, Any]]:
        """
        Normalize the existing Lead.attributes JSON.

        No duplicate attribute state is created.
        """

        raw_attributes = (
            lead.attributes or {}
        )

        if not isinstance(
            raw_attributes,
            dict,
        ):
            return []

        return [
            {
                "name": str(
                    name
                ),
                "value": value,
            }
            for name, value in raw_attributes.items()
        ]

    # ============================================================
    # WHATSAPP CONVERSATION
    # ============================================================

    def _get_messages(
        self,
        *,
        organization,
        lead: Lead,
        limit: int,
    ) -> list[WhatsAppMessage]:
        """
        Retrieve recent WhatsApp messages.

        Organization and Lead are both explicitly scoped.
        """

        messages = list(
            WhatsAppMessage.objects
            .filter(
                organization=organization,
                lead=lead,
            )
            .order_by(
                "-created_at",
                "-id",
            )[:limit]
        )

        messages.reverse()

        return messages

    def _build_conversation_context(
        self,
        *,
        messages: list[WhatsAppMessage],
    ) -> dict[str, Any]:
        """
        Build normalized conversation context.
        """

        normalized_messages = []

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
                speaker = "lead"
            else:
                speaker = "shvya"

            normalized_messages.append(
                {
                    "id": str(
                        message.id
                    ),
                    "direction": message.direction,
                    "speaker": speaker,
                    "body": body,
                    "status": message.status,
                    "created_at": (
                        message.created_at.isoformat()
                        if message.created_at
                        else None
                    ),
                }
            )

        return {
            "message_count": len(
                normalized_messages
            ),
            "messages": normalized_messages,
        }

    # ============================================================
    # INTERNAL CONVERSATION SUMMARY
    # ============================================================

    def _build_conversation_summary(
        self,
        *,
        organization,
        lead: Lead,
    ) -> dict[str, Any] | None:
        """
        Read the currently active Internal Conversation Summary.

        IMPORTANT:

            This is the summary of the actual chat.

            It is NOT the Qualification Summary.

        UI:

            Lead Card
                ↓
            Conversation Summary icon
                ↓
            Read-only summary
        """

        summary = (
            InternalConversationSummary.objects
            .filter(
                organization=organization,
                lead=lead,
                is_active=True,
            )
            .order_by(
                "-generated_at",
            )
            .first()
        )

        if summary is None:
            return None

        return {
            "id": str(
                summary.id
            ),
            "summary": summary.summary,
            "source_message_count": (
                summary.source_message_count
            ),
            "generated_by": summary.generated_by,
            "model_name": summary.model_name,
            "generated_at": (
                summary.generated_at.isoformat()
                if summary.generated_at
                else None
            ),
            "updated_at": (
                summary.updated_at.isoformat()
                if summary.updated_at
                else None
            ),
        }

    # ============================================================
    # QUALIFICATION SUMMARY / LEAD NOTES
    # ============================================================

    def _build_qualification_notes(
        self,
        *,
        lead: Lead,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        Read the existing LeadNote records.

        Qualification Summary belongs to the Lead Note system.

        It is intentionally returned separately from
        conversation_summary.
        """

        notes = list(
            lead.lead_notes
            .select_related(
                "created_by",
            )
            .order_by(
                "-created_at",
                "-id",
            )[:limit]
        )

        return [
            {
                "id": str(
                    note.id
                ),
                "note": note.note,
                "note_type": note.note_type,
                "created_by": (
                    str(
                        note.created_by_id
                    )
                    if note.created_by_id
                    else None
                ),
                "created_at": (
                    note.created_at.isoformat()
                    if note.created_at
                    else None
                ),
                "updated_at": (
                    note.updated_at.isoformat()
                    if note.updated_at
                    else None
                ),
            }
            for note in notes
        ]

    # ============================================================
    # KNOWLEDGE
    # ============================================================

    def _build_knowledge_context(
        self,
        *,
        organization,
        knowledge_query: str | None,
        query_vector: list[float] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        Retrieve relevant Knowledge Base context.

        Resolution order:

            1. query_vector, if the caller already computed one.

            2. knowledge_query, embedded here via EmbeddingService.

            3. Neither supplied, or embedding fails: return no
               knowledge rather than fabricate a vector.
        """

        if query_vector is None:

            normalized_query = (
                knowledge_query or ""
            ).strip()

            if not normalized_query:
                return []

            try:
                query_vector = (
                    EmbeddingService().embed_text(
                        normalized_query
                    )
                )
            except EmbeddingError:
                # No provider key, or the provider call failed:
                # degrade to no knowledge rather than break the
                # whole AI context build.
                return []

        results = (
            KnowledgeRetrievalService()
            .retrieve_by_vector(
                organization=organization,
                query_vector=query_vector,
                limit=limit,
            )
        )

        return [
            {
                "chunk_id": str(
                    result.chunk.id
                ),
                "document_id": str(
                    result.chunk.document_id
                ),
                "document_name": (
                    result.chunk.document.name
                ),
                "document_version": (
                    result.chunk.document.version
                ),
                "content": result.chunk.content,
                "similarity": result.similarity,
                "distance": result.distance,
            }
            for result in results
        ]

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
            raise AIContextError(
                "Organization is required."
            )

        if lead is None:
            raise AIContextError(
                "Lead is required."
            )

        if lead.organization_id != organization.id:
            raise AIContextError(
                "Lead does not belong to this organization."
            )

    def _validate_message_limit(
        self,
        limit: int,
    ) -> int:
        """
        Validate conversation history size.
        """

        if limit <= 0:
            raise AIContextError(
                "Message limit must be greater than zero."
            )

        if limit > self.MAX_MESSAGE_LIMIT:
            raise AIContextError(
                f"Message limit cannot exceed "
                f"{self.MAX_MESSAGE_LIMIT}."
            )

        return limit

    def _validate_knowledge_limit(
        self,
        limit: int,
    ) -> int:
        """
        Validate retrieval size.
        """

        if limit <= 0:
            raise AIContextError(
                "Knowledge limit must be greater than zero."
            )

        if limit > self.MAX_KNOWLEDGE_LIMIT:
            raise AIContextError(
                f"Knowledge limit cannot exceed "
                f"{self.MAX_KNOWLEDGE_LIMIT}."
            )

        return limit

    def _validate_note_limit(
        self,
        limit: int,
    ) -> int:
        """
        Validate Lead Note history size.
        """

        if limit <= 0:
            raise AIContextError(
                "Note limit must be greater than zero."
            )

        if limit > self.MAX_NOTE_LIMIT:
            raise AIContextError(
                f"Note limit cannot exceed "
                f"{self.MAX_NOTE_LIMIT}."
            )

        return limit