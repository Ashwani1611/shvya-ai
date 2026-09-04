from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.ai_engagement.services.ai_provider import (
    AIProviderError,
    OpenAIProvider,
)
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.embeddings import (
    EmbeddingError,
    EmbeddingService,
)
from apps.ai_engagement.services.engagement import (
    EngagementError,
    EngagementService,
)
from apps.ai_engagement.services.org_info import OrgInfoService
from apps.ai_engagement.services.retrieval import (
    KnowledgeRetrievalService,
)


class PlaygroundError(Exception):
    """
    Raised when the AI Playground request cannot be completed safely.
    """


@dataclass(frozen=True)
class PlaygroundResult:
    """
    Normalized result for one Playground turn.
    """

    session_id: str
    message: str
    response: str
    should_engage: bool
    knowledge: list[dict[str, Any]]
    model: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "message": self.message,
            "response": self.response,
            "should_engage": self.should_engage,
            "knowledge": self.knowledge,
            "model": self.model,
        }


class PlaygroundService:
    """
    Isolated AI Playground execution service.

    The Playground uses the same organization configuration,
    Knowledge/RAG services, Base Instructions, and EngagementService
    normalization as production AI Engagement.

    It intentionally does NOT:
        - resolve a Lead
        - mutate CRM
        - create LeadNotes
        - create reminders
        - move pipeline stages
        - update contacts
        - update lead attributes
        - send WhatsApp messages
        - call Meta

    Conversation history is supplied by the API caller and normalized
    here. It is not persisted as CRM conversation data.
    """

    MAX_HISTORY_MESSAGES = 40
    MAX_MESSAGE_LENGTH = 4000
    MAX_KNOWLEDGE = 5

    def __init__(
        self,
        *,
        provider: OpenAIProvider | None = None,
        engagement_service: EngagementService | None = None,
        org_info_service: OrgInfoService | None = None,
        embedding_service: EmbeddingService | None = None,
        retrieval_service: KnowledgeRetrievalService | None = None,
    ) -> None:
        self.provider = provider

        self.engagement_service = (
            engagement_service
            or EngagementService(
                provider=provider,
            )
        )

        self.org_info_service = (
            org_info_service
            or OrgInfoService()
        )

        self.embedding_service = (
            embedding_service
            or EmbeddingService()
        )

        self.retrieval_service = (
            retrieval_service
            or KnowledgeRetrievalService()
        )

    # ============================================================
    # PUBLIC API
    # ============================================================

    def run(
        self,
        *,
        organization,
        session_id: str,
        message: str,
        history: list[dict[str, Any]] | None = None,
    ) -> PlaygroundResult:
        """
        Execute one isolated Playground turn.
        """

        if organization is None:
            raise PlaygroundError(
                "Organization is required."
            )

        session_id = str(
            session_id or ""
        ).strip()

        if not session_id:
            raise PlaygroundError(
                "session_id is required."
            )

        message = self._normalize_message(
            message
        )

        conversation = self._normalize_history(
            history=history,
            current_message=message,
        )

        # --------------------------------------------------------
        # ORGANIZATION AI CONFIGURATION
        # --------------------------------------------------------

        org_info = (
            self.org_info_service.get_or_create(
                organization=organization,
            )
        )

        if not org_info.ai_enabled:
            raise PlaygroundError(
                "AI is disabled for this organization."
            )

        # --------------------------------------------------------
        # KNOWLEDGE
        # --------------------------------------------------------

        knowledge = self._retrieve_knowledge(
            organization=organization,
            query=message,
        )

        # --------------------------------------------------------
        # RUNTIME CONTEXT
        #
        # This is an isolated Playground context.
        #
        # No Lead object is loaded and no CRM state is mutated.
        # --------------------------------------------------------

        context = AIContext(
            organization={
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
                "engagement_instructions": (
                    org_info.engagement_instructions
                ),
                "bump_up_enabled": (
                    org_info.bump_up_enabled
                ),
                "bump_up_count": (
                    org_info.bump_up_count
                ),
            },
            lead={
                "id": (
                    f"playground:{session_id}"
                ),
                "name": "Playground Visitor",
                "phone": "",
                "email": "",
                "notes": "",
                "attributes": {},
                "lead_source": "playground",
                "stage_entered_at": None,
                "created_at": None,
                "updated_at": None,
            },
            pipeline={},
            stage={},
            contacts=[],
            attributes=[],
            conversation={
                "message_count": len(
                    conversation
                ),
                "messages": conversation,
            },
            conversation_summary=None,
            qualification_notes=[],
            knowledge=knowledge,
        )

        # --------------------------------------------------------
        # SAME ENGAGEMENT ENGINE
        #
        # Reuse:
        #   - Base System Instructions
        #   - organization engagement instructions
        #   - Engagement task instructions
        #   - provider abstraction
        #   - normalized EngagementDecision
        #
        # We deliberately do NOT call EngagementService.engage()
        # because that production method expects a real Lead.
        # --------------------------------------------------------

        try:
            instructions = (
                self.engagement_service._build_instructions(
                    context=context,
                )
            )

            input_text = (
                self.engagement_service._build_input(
                    context=context,
                )
            )

            provider = (
                self.provider
                or OpenAIProvider()
            )

            result = provider.generate_text(
                instructions=instructions,
                input_text=input_text,
                metadata={
                    "organization_id": str(
                        organization.id
                    ),
                    "session_id": session_id,
                    "task": "playground",
                },
            )

            decision = (
                self.engagement_service._normalize_result(
                    result=result,
                )
            )

        except AIProviderError as exc:
            raise PlaygroundError(
                "AI Playground generation failed."
            ) from exc

        except EngagementError as exc:
            raise PlaygroundError(
                str(exc)
            ) from exc

        # --------------------------------------------------------
        # EXTERNAL PLAYGROUND RESULT
        #
        # CRM action requests are deliberately NOT returned.
        # Playground is only testing the conversational result.
        # --------------------------------------------------------

        return PlaygroundResult(
            session_id=session_id,
            message=message,
            response=(
                decision.message
                if decision.should_engage
                else ""
            ),
            should_engage=(
                decision.should_engage
            ),
            knowledge=knowledge,
            model=decision.model,
        )

    # ============================================================
    # MESSAGE NORMALIZATION
    # ============================================================

    def _normalize_message(
        self,
        message: str,
    ) -> str:
        message = (
            message or ""
        ).strip()

        if not message:
            raise PlaygroundError(
                "message is required."
            )

        return message[
            : self.MAX_MESSAGE_LENGTH
        ]

    # ============================================================
    # CONVERSATION
    # ============================================================

    def _normalize_history(
        self,
        *,
        history: list[dict[str, Any]] | None,
        current_message: str,
    ) -> list[dict[str, Any]]:
        """
        Normalize caller-supplied Playground history.

        Only user/assistant messages are accepted.
        """

        normalized: list[
            dict[str, Any]
        ] = []

        history_items = (
            history or []
        )[
            -self.MAX_HISTORY_MESSAGES :
        ]

        for item in history_items:

            if not isinstance(
                item,
                dict,
            ):
                continue

            role = str(
                item.get("role")
                or ""
            ).strip().lower()

            body = str(
                item.get("content")
                or item.get("message")
                or ""
            ).strip()

            if role not in {
                "user",
                "assistant",
            }:
                continue

            if not body:
                continue

            normalized.append(
                {
                    "id": str(
                        item.get("id")
                        or ""
                    ),
                    "direction": (
                        "inbound"
                        if role == "user"
                        else "outbound"
                    ),
                    "speaker": (
                        "lead"
                        if role == "user"
                        else "shvya"
                    ),
                    "body": body[
                        : self.MAX_MESSAGE_LENGTH
                    ],
                    "status": "playground",
                    "created_at": item.get(
                        "created_at"
                    ),
                }
            )

        # Current user message is always appended as the latest
        # message in the conversation.
        normalized.append(
            {
                "id": "",
                "direction": "inbound",
                "speaker": "lead",
                "body": current_message,
                "status": "playground",
                "created_at": None,
            }
        )

        return normalized

    # ============================================================
    # KNOWLEDGE
    # ============================================================

    def _retrieve_knowledge(
        self,
        *,
        organization,
        query: str,
    ) -> list[dict[str, Any]]:
        """
        Reuse the existing embedding + organization-scoped
        KnowledgeRetrievalService.

        If embedding cannot be generated, Playground continues
        without Knowledge rather than inventing content.
        """

        try:
            query_vector = (
                self.embedding_service.embed_text(
                    query
                )
            )

        except EmbeddingError:
            return []

        results = (
            self.retrieval_service.retrieve_by_vector(
                organization=organization,
                query_vector=query_vector,
                limit=self.MAX_KNOWLEDGE,
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