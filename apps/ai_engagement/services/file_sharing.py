from __future__ import annotations

import json
import re
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

        # Once an organization configures guided files, only those files are
        # candidates. Before that, preserve compatibility with existing
        # knowledge-file installations.
        if queryset.exclude(share_instruction="").exists():
            queryset = queryset.exclude(share_instruction="")

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
        """Build an organization-scoped allow-list for guided file sharing.

        Normally candidates come from verified RAG chunks. When the latest lead
        message explicitly asks for a file/brochure/catalogue/document, also
        expose configured eligible files so a missing semantic chunk cannot make
        a real uploaded file impossible to send. The response model still must
        match share_instruction and may select only an ID in this allow-list.
        """
        knowledge_items = context.as_dict().get("knowledge", [])
        document_ids = {
            int(item["document_id"])
            for item in knowledge_items
            if item.get("document_id") is not None
        }
        latest_text = ""
        conversation = context.as_dict().get("conversation", {})
        for message in reversed(conversation.get("messages", []) or []):
            if isinstance(message, dict) and message.get("direction") == "inbound":
                latest_text = str(message.get("body") or "").strip()
                if latest_text:
                    break
        explicit_file_request = bool(re.search(
            r"(?:brochure|catalog(?:ue)?|pdf|file|document|deck|presentation|menu|prospectus|portfolio|flyer|leaflet|datasheet|price\s*list|pricelist)",
            latest_text,
            flags=re.IGNORECASE,
        ))
        if not document_ids and not explicit_file_request:
            return []

        retrieved_documents = (
            self.get_eligible_documents(
                organization=organization,
                document_ids=document_ids,
            )
            if document_ids
            else []
        )
        documents = list(retrieved_documents)
        if explicit_file_request:
            seen = {document.id for document in documents}
            for document in self.get_eligible_documents(organization=organization):
                if document.id not in seen:
                    documents.append(document)
                    seen.add(document.id)
                if len(documents) >= 10:
                    break

        evidence_by_id: dict[int, dict[str, Any]] = {}
        for item in knowledge_items:
            raw_id = item.get("document_id")
            if raw_id is None:
                continue
            document_id = int(raw_id)
            existing = evidence_by_id.get(document_id)
            if existing is None or float(item.get("similarity", 0.0)) > float(existing.get("similarity", 0.0)):
                evidence_by_id[document_id] = item

        candidates = []
        for document in documents[:10]:
            item = evidence_by_id.get(document.id, {})
            candidates.append({
                "document_id": document.id,
                "name": document.name,
                "version": document.version,
                "source_url": document.source_url,
                "share_instruction": document.share_instruction,
                "relevance": float(item.get("similarity", 0.0)),
                "evidence": str(item.get("content") or "").strip(),
            })
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
                        (
                            f"When and why to send: "
                            f"{candidate['share_instruction']}"
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
