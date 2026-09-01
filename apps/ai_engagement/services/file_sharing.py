from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.ai_provider import (
    AIProviderError,
    AIProviderTransientError,
    OpenAIProvider,
)
from apps.ai_engagement.services.context import (
    AIContextBuilder,
    AIContextError,
)


class FileSharingError(Exception):
    """
    Raised when AI-guided file sharing cannot be completed safely.
    """


@dataclass(frozen=True)
class FileSharingDecision:
    """
    Normalized file-sharing decision returned by the service.
    """

    should_share: bool
    document_id: int | None
    reason: str
    model: str


class FileSharingService:
    """
    Determines whether an existing organization-owned file should
    be shared with a lead.

    This service does not send files or messages.

    Flow:

        centralized AI context
            ↓
        retrieved knowledge
            ↓
        eligible file candidates
            ↓
        AI decision
            ↓
        deterministic validation
            ↓
        FileSharingDecision
    """

    FILE_SHARING_INSTRUCTIONS = """
You are SHVYA AI's file-sharing decision assistant.

Your job is to determine whether one of the provided organization
files is sufficiently relevant to the lead's current conversation
that SHVYA should consider sharing it.

IMPORTANT RULES:

1. Use the actual conversation as the primary evidence.
2. Use the supplied CRM context and retrieved knowledge as supporting
   context.
3. Do not invent facts.
4. Do not invent document IDs.
5. You may select ONLY a document ID explicitly present in the
   FILE CANDIDATES section.
6. Share a file only when it is genuinely useful and relevant to the
   current conversation.
7. If no file is sufficiently relevant, do not select a file.
8. Do not write a customer-facing message.
9. Do not send anything.
10. Return ONLY valid JSON.
11. The JSON must contain exactly these fields:

{
  "should_share": true or false,
  "document_id": integer or null,
  "reason": "concise explanation"
}

Rules for the fields:

- If should_share is false, document_id MUST be null.
- If should_share is true, document_id MUST be one of the supplied
  candidate document IDs.
- reason must briefly explain the decision.
""".strip()

    # ========================================================
    # AI CONTEXT
    # ========================================================

    def build_ai_context(
        self,
        *,
        organization,
        lead,
    ):
        """
        Build the centralized SHVYA AI context.
        """

        try:
            return AIContextBuilder().build(
                organization=organization,
                lead=lead,
                query_vector=None,
                message_limit=100,
                knowledge_limit=5,
            )

        except AIContextError as exc:
            raise FileSharingError(
                f"Unable to build file-sharing context: {exc}"
            ) from exc

    # ========================================================
    # ELIGIBLE DOCUMENTS
    # ========================================================

    def get_eligible_documents(
        self,
        *,
        organization,
        document_ids: set[int] | None = None,
    ) -> list[Document]:
        """
        Return organization-owned, active, completed documents
        that contain an uploaded file.
        """

        queryset = (
            Document.objects
            .filter(
                organization=organization,
                is_active=True,
                processing_status=Document.ProcessingStatus.COMPLETED,
            )
            .exclude(
                file="",
            )
            .order_by(
                "-updated_at",
                "-id",
            )
        )

        if document_ids is not None:
            if not document_ids:
                return []

            queryset = queryset.filter(
                id__in=document_ids,
            )

        return list(queryset)

    # ========================================================
    # CANDIDATES
    # ========================================================

    def build_file_candidates(
        self,
        *,
        organization,
        context,
    ) -> list[dict[str, Any]]:
        """
        Convert retrieved knowledge documents into unique,
        organization-scoped file candidates.
        """

        knowledge_items = (
            context.as_dict().get(
                "knowledge",
                [],
            )
        )

        document_ids = {
            int(item["document_id"])
            for item in knowledge_items
            if item.get("document_id") is not None
        }

        if not document_ids:
            return []

        documents = self.get_eligible_documents(
            organization=organization,
            document_ids=document_ids,
        )

        documents_by_id = {
            document.id: document
            for document in documents
        }

        candidates = []

        seen_ids = set()

        for item in knowledge_items:
            raw_document_id = item.get(
                "document_id"
            )

            if raw_document_id is None:
                continue

            document_id = int(
                raw_document_id
            )

            if document_id in seen_ids:
                continue

            document = documents_by_id.get(
                document_id
            )

            if document is None:
                continue

            seen_ids.add(
                document_id
            )

            candidates.append(
                {
                    "document_id": document.id,
                    "name": document.name,
                    "version": document.version,
                    "source_url": document.source_url,
                    "relevance": float(
                        item.get(
                            "similarity",
                            0.0,
                        )
                    ),
                    "evidence": (
                        item.get(
                            "content",
                            "",
                        )
                        or ""
                    ).strip(),
                }
            )

        return candidates

    # ========================================================
    # PROVIDER INPUT
    # ========================================================

    def build_provider_input(
        self,
        *,
        organization,
        lead,
    ) -> str:
        """
        Build bounded provider input from centralized context.
        """

        context = self.build_ai_context(
            organization=organization,
            lead=lead,
        )

        context_data = context.as_dict()

        organization_data = (
            context_data["organization"]
        )

        lead_data = (
            context_data["lead"]
        )

        conversation_data = (
            context_data["conversation"]
        )

        conversation_summary = (
            context_data.get(
                "conversation_summary"
            )
        )

        candidates = self.build_file_candidates(
            organization=organization,
            context=context,
        )

        lines = [
            "SHVYA AI FILE SHARING INPUT",
            "",
            "ORGANIZATION",
            f"Name: {organization_data.get('name', '')}",
            f"About: {organization_data.get('about', '')}",
            "",
            "LEAD",
            f"Name: {lead_data.get('name', '')}",
            f"Lead Source: {lead_data.get('lead_source', '')}",
            f"Notes: {lead_data.get('notes', '')}",
            f"Attributes: {lead_data.get('attributes', {})}",
            "",
        ]

        if conversation_summary:
            lines.extend(
                [
                    "CURRENT CONVERSATION SUMMARY",
                    (
                        conversation_summary.get(
                            "summary",
                            "",
                        )
                        or ""
                    ).strip(),
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "CURRENT CONVERSATION SUMMARY",
                    "No current conversation summary is available.",
                    "",
                ]
            )

        lines.extend(
            [
                "ACTUAL CONVERSATION",
                (
                    f"Message count: "
                    f"{conversation_data.get('message_count', 0)}"
                ),
                "",
            ]
        )

        for message in conversation_data.get(
            "messages",
            [],
        ):
            speaker = (
                "Lead"
                if message.get("direction") == "inbound"
                else "SHVYA"
            )

            body = (
                message.get("body")
                or ""
            ).strip()

            if body:
                lines.append(
                    f"{speaker}: {body}"
                )

        lines.extend(
            [
                "",
                "FILE CANDIDATES",
            ]
        )

        if not candidates:
            lines.append(
                "No eligible file candidates are available."
            )
        else:
            for candidate in candidates:
                lines.extend(
                    [
                        (
                            f"Document ID: "
                            f"{candidate['document_id']}"
                        ),
                        (
                            f"Name: "
                            f"{candidate['name']}"
                        ),
                        (
                            f"Version: "
                            f"{candidate['version']}"
                        ),
                        (
                            f"Relevance: "
                            f"{candidate['relevance']}"
                        ),
                        (
                            f"Evidence: "
                            f"{candidate['evidence']}"
                        ),
                        "",
                    ]
                )

        return "\n".join(
            lines
        ).strip()

    # ========================================================
    # AI GENERATION
    # ========================================================

    def generate(
        self,
        *,
        organization,
        lead,
    ) -> FileSharingDecision:
        """
        Generate and validate an AI file-sharing decision.
        """

        provider_input = self.build_provider_input(
            organization=organization,
            lead=lead,
        )

        if not provider_input:
            raise FileSharingError(
                "File-sharing provider input is empty."
            )

        try:
            provider = OpenAIProvider()

            result = provider.generate_text(
                instructions=self.FILE_SHARING_INSTRUCTIONS,
                input_text=provider_input,
                metadata={
                    "organization_id": str(
                        organization.id
                    ),
                    "lead_id": str(
                        lead.id
                    ),
                    "purpose": "ai_file_sharing_decision",
                },
            )

        except AIProviderTransientError:
            raise

        except AIProviderError as exc:
            raise FileSharingError(
                "AI file-sharing generation failed: "
                f"{exc}"
            ) from exc

        return self.parse_decision(
            organization=organization,
            lead=lead,
            raw_text=result.text,
            model=result.model,
        )

    # ========================================================
    # PARSING
    # ========================================================

    def parse_decision(
        self,
        *,
        organization,
        lead,
        raw_text: str,
        model: str,
    ) -> FileSharingDecision:
        """
        Parse and deterministically validate provider JSON.
        """

        raw_text = (
            raw_text or ""
        ).strip()

        if not raw_text:
            raise FileSharingError(
                "AI provider returned an empty file-sharing decision."
            )

        try:
            payload = json.loads(
                raw_text
            )

        except json.JSONDecodeError as exc:
            raise FileSharingError(
                "AI file-sharing response is not valid JSON."
            ) from exc

        if not isinstance(
            payload,
            dict,
        ):
            raise FileSharingError(
                "AI file-sharing response must be a JSON object."
            )

        expected_keys = {
            "should_share",
            "document_id",
            "reason",
        }

        if set(payload.keys()) != expected_keys:
            raise FileSharingError(
                "AI file-sharing response contains an invalid schema."
            )

        should_share = payload[
            "should_share"
        ]

        document_id = payload[
            "document_id"
        ]

        reason = (
            payload["reason"]
            or ""
        ).strip()

        if not isinstance(
            should_share,
            bool,
        ):
            raise FileSharingError(
                "should_share must be a boolean."
            )

        if not reason:
            raise FileSharingError(
                "File-sharing decision reason cannot be empty."
            )

        if not should_share:
            if document_id is not None:
                raise FileSharingError(
                    "document_id must be null when should_share is false."
                )

            return FileSharingDecision(
                should_share=False,
                document_id=None,
                reason=reason,
                model=model,
            )

        if isinstance(
            document_id,
            bool,
        ) or not isinstance(
            document_id,
            int,
        ):
            raise FileSharingError(
                "document_id must be an integer when should_share is true."
            )

        eligible_documents = self.get_eligible_documents(
            organization=organization,
            document_ids={
                document_id,
            },
        )

        if not eligible_documents:
            raise FileSharingError(
                "AI selected a document that is not an eligible "
                "organization-owned file."
            )

        return FileSharingDecision(
            should_share=True,
            document_id=document_id,
            reason=reason,
            model=model,
        )