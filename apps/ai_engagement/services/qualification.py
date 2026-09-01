from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.services.ai_provider import (
    AIProviderError,
    AIProviderTransientError,
    OpenAIProvider,
)
from apps.ai_engagement.services.context import (
    AIContextBuilder,
    AIContextError,
)
from apps.crm.models import Lead, LeadNote


class QualificationError(Exception):
    """
    Raised when AI qualification cannot be generated or persisted.
    """


@dataclass(frozen=True)
class QualificationResult:
    """
    Normalized qualification result returned by the qualification
    service.
    """

    summary: str
    model: str


class QualificationService:
    """
    Generates an internal AI qualification summary for a Lead.

    Qualification Summary is intentionally separate from the
    internal Conversation Summary.

    Conversation Summary:
        What happened in the conversation?

    Qualification Summary:
        What does the conversation tell SHVYA about this Lead's
        qualification against the organization's requirements?

    AI context priority:

        1. Actual conversation
        2. Organization qualification requirements
        3. Lead / CRM context
        4. Current Conversation Summary
        5. Existing Qualification Summary history

    The Conversation Summary is supporting context. It must not
    override newer evidence from the actual conversation.

    Persistence:

        LeadNote(note_type="system")

    Manual LeadNotes are never modified.
    """

    QUALIFICATION_HEADER = (
        "AI Qualification Summary"
    )

    QUALIFICATION_HEADER_PREFIX = (
        "<AI Qualification Summary"
    )

    UPDATED_MARKER = (
        "***** Updated Summary"
    )

    QUALIFICATION_INSTRUCTIONS = """
You are SHVYA AI's internal lead-qualification analyst.

Your job is to assess a lead against the organization's
qualification requirements using the supplied CRM and
conversation context.

This result is for internal CRM users only.

IMPORTANT EVIDENCE PRIORITY:

1. The actual conversation is the primary source of truth.
2. The organization's qualification requirements define what
   should be evaluated.
3. Lead and CRM data provide structured context.
4. The current Conversation Summary is supporting context that
   helps you understand the broader conversation.
5. Existing qualification summaries provide historical context.

If the current Conversation Summary conflicts with newer messages
in the actual conversation, trust the newer actual conversation.

Do not blindly repeat an older qualification conclusion.

Do not invent facts.

Do not infer unsupported personal information.

Do not write a customer-facing reply.

Do not modify CRM fields.

Do not make decisions that are unsupported by the organization's
qualification requirements or supplied evidence.

Focus on:

- confirmed qualification signals
- missing qualification information
- unclear information
- objections or blockers
- relevant preferences or requirements
- buying intent when supported
- timeline when supported
- budget when supported
- decision factors when supported
- current qualification assessment

The output must be a concise internal CRM qualification summary
in clear prose.

Do not use JSON.
Do not use markdown tables.
Do not write a conversation transcript.
"""

    # ========================================================
    # AI CONTEXT
    # ========================================================

    def build_ai_context(
        self,
        *,
        organization,
        lead: Lead,
    ):
        """
        Build centralized SHVYA AI context.

        Qualification uses the same centralized context as the
        rest of the AI layer.

        This context already includes:

            - organization
            - qualification requirements
            - lead
            - pipeline
            - stage
            - contacts
            - attributes
            - recent conversation
            - current conversation summary
            - existing qualification notes
            - knowledge context
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

            raise QualificationError(
                f"Unable to build qualification context: {exc}"
            ) from exc

    # ========================================================
    # PROVIDER INPUT
    # ========================================================

    def build_provider_input(
        self,
        *,
        organization,
        lead: Lead,
    ) -> str:
        """
        Convert the centralized AI context into bounded provider
        input.

        The actual conversation remains the authoritative evidence.

        The current Conversation Summary is explicitly supplied as
        supporting context so qualification can understand the
        broader conversation without depending on the summary as
        the source of truth.
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

        pipeline_data = (
            context_data["pipeline"]
        )

        stage_data = (
            context_data["stage"]
        )

        conversation_data = (
            context_data["conversation"]
        )

        conversation_summary = (
            context_data.get(
                "conversation_summary"
            )
        )

        qualification_notes = (
            context_data["qualification_notes"]
        )

        lines = [
            "SHVYA AI QUALIFICATION INPUT",
            "",
            "EVIDENCE PRIORITY",
            (
                "The actual conversation is the primary source "
                "of truth. The current conversation summary is "
                "supporting context only."
            ),
            "",
            "ORGANIZATION",
            (
                f"Name: "
                f"{organization_data.get('name', '')}"
            ),
            (
                f"About: "
                f"{organization_data.get('about', '')}"
            ),
            (
                "Qualification Requirements: "
                f"{organization_data.get('qualification_requirements', '')}"
            ),
            "",
            "LEAD",
            (
                f"Name: "
                f"{lead_data.get('name', '')}"
            ),
            (
                f"Phone: "
                f"{lead_data.get('phone', '')}"
            ),
            (
                f"Email: "
                f"{lead_data.get('email', '')}"
            ),
            (
                f"Lead Source: "
                f"{lead_data.get('lead_source', '')}"
            ),
            (
                f"Notes: "
                f"{lead_data.get('notes', '')}"
            ),
            (
                f"Attributes: "
                f"{lead_data.get('attributes', {})}"
            ),
            "",
            "PIPELINE",
            (
                f"Name: "
                f"{pipeline_data.get('name', '')}"
            ),
            (
                "Description: "
                f"{pipeline_data.get('description', '')}"
            ),
            "",
            "STAGE",
            (
                f"Name: "
                f"{stage_data.get('name', '')}"
            ),
            (
                "Description: "
                f"{stage_data.get('description', '')}"
            ),
            "",
            "CURRENT CONVERSATION SUMMARY",
        ]

        # ----------------------------------------------------
        # CURRENT CONVERSATION SUMMARY
        # ----------------------------------------------------

        if conversation_summary:

            summary_text = (
                conversation_summary.get(
                    "summary"
                )
                or ""
            ).strip()

            if summary_text:

                lines.extend(
                    [
                        (
                            "Generated: "
                            f"{conversation_summary.get('generated_at', '')}"
                        ),
                        (
                            "Message Count Covered: "
                            f"{conversation_summary.get('source_message_count', 0)}"
                        ),
                        (
                            "Model: "
                            f"{conversation_summary.get('model_name', '')}"
                        ),
                        (
                            "Summary: "
                            f"{summary_text}"
                        ),
                    ]
                )

            else:

                lines.append(
                    "No current conversation summary is available."
                )

        else:

            lines.append(
                "No current conversation summary is available."
            )

        # ----------------------------------------------------
        # ACTUAL CONVERSATION
        # ----------------------------------------------------

        lines.extend(
            [
                "",
                "ACTUAL CONVERSATION",
                (
                    f"Message count: "
                    f"{conversation_data.get('message_count', 0)}"
                ),
                "",
            ]
        )

        conversation_messages = (
            conversation_data.get(
                "messages",
                [],
            )
        )

        for message in conversation_messages:

            timestamp = (
                message.get("created_at")
                or ""
            )

            direction = (
                message.get("direction")
                or ""
            )

            speaker = (
                "Lead"
                if direction == "inbound"
                else "SHVYA"
            )

            body = (
                message.get("body")
                or ""
            ).strip()

            if body:

                lines.append(
                    f"[{timestamp}] {speaker}: {body}"
                )

        # ----------------------------------------------------
        # EXISTING QUALIFICATION HISTORY
        # ----------------------------------------------------

        lines.extend(
            [
                "",
                "EXISTING QUALIFICATION HISTORY",
            ]
        )

        qualification_history_found = False

        for note in qualification_notes:

            note_text = (
                note.get("note")
                or ""
            ).strip()

            note_type = (
                note.get("note_type")
                or ""
            )

            if (
                note_text
                and note_type == "system"
            ):

                qualification_history_found = True

                lines.append(
                    note_text
                )

        if not qualification_history_found:

            lines.append(
                "No previous AI qualification history."
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
        lead: Lead,
    ) -> QualificationResult:
        """
        Generate a qualification assessment through the
        configured AI provider.

        Transient provider failures are deliberately allowed
        to propagate unchanged so the Celery task can retry them.

        Permanent/configuration provider failures are converted
        into QualificationError and are not retried by the task.
        """

        provider_input = (
            self.build_provider_input(
                organization=organization,
                lead=lead,
            )
        )

        if not provider_input:

            raise QualificationError(
                "Qualification provider input is empty."
            )

        try:

            provider = OpenAIProvider()

            result = provider.generate_text(
                instructions=(
                    self.QUALIFICATION_INSTRUCTIONS
                ),
                input_text=provider_input,
                metadata={
                    "organization_id": str(
                        organization.id
                    ),
                    "lead_id": str(
                        lead.id
                    ),
                    "purpose": (
                        "lead_qualification_summary"
                    ),
                },
            )

        except AIProviderTransientError:
            raise

        except AIProviderError as exc:

            raise QualificationError(
                "AI qualification generation failed: "
                f"{exc}"
            ) from exc

        summary = (
            result.text or ""
        ).strip()

        if not summary:

            raise QualificationError(
                "AI provider returned an empty qualification summary."
            )

        return QualificationResult(
            summary=summary,
            model=result.model,
        )

    # ========================================================
    # CURRENT AI QUALIFICATION NOTE
    # ========================================================

    def get_current_ai_note(
        self,
        *,
        lead: Lead,
    ) -> LeadNote | None:
        """
        Return the current cumulative AI qualification system note.

        Manual notes and unrelated system notes are excluded.
        """

        return (
            LeadNote.objects
            .filter(
                lead=lead,
                note_type="system",
            )
            .filter(
                note__startswith=(
                    self.QUALIFICATION_HEADER_PREFIX
                )
            )
            .order_by(
                "-created_at",
                "-id",
            )
            .first()
        )

    # ========================================================
    # NORMALIZATION
    # ========================================================

    def _normalize_summary(
        self,
        summary: str,
    ) -> str:
        """
        Normalize AI output before comparison/persistence.
        """

        return (
            summary or ""
        ).strip()

    # ========================================================
    # EXTRACT LATEST SUMMARY
    # ========================================================

    def _extract_latest_summary(
        self,
        note_text: str,
    ) -> str:
        """
        Extract the latest qualification assessment from the
        cumulative AI qualification note.

        Supported formats:

            <AI Qualification Summary - ...>
            initial summary

            ***** Updated Summary DD/MM HH:MM *****
            updated summary
        """

        value = (
            note_text or ""
        ).strip()

        if not value:
            return ""

        marker_positions = []

        search_from = 0

        while True:

            position = value.find(
                self.UPDATED_MARKER,
                search_from,
            )

            if position == -1:
                break

            marker_positions.append(
                position
            )

            search_from = (
                position
                + len(self.UPDATED_MARKER)
            )

        # ----------------------------------------------------
        # INITIAL SUMMARY ONLY
        # ----------------------------------------------------

        if not marker_positions:

            header_end = value.find(
                ">"
            )

            if header_end == -1:

                return value

            return (
                value[
                    header_end + 1:
                ]
                .strip()
            )

        # ----------------------------------------------------
        # LATEST UPDATED SUMMARY
        # ----------------------------------------------------

        latest_marker = (
            marker_positions[-1]
        )

        latest = (
            value[
                latest_marker:
            ]
        )

        lines = (
            latest.splitlines()
        )

        content_lines = []

        for line in lines[1:]:

            stripped = (
                line.strip()
            )

            if stripped:

                content_lines.append(
                    stripped
                )

        return "\n".join(
            content_lines
        ).strip()

    # ========================================================
    # MEANINGFUL CHANGE
    # ========================================================

    def has_meaningful_change(
        self,
        *,
        existing_note: LeadNote | None,
        new_summary: str,
    ) -> bool:
        """
        Determine whether the newly-generated qualification
        differs from the latest stored qualification.
        """

        normalized_new = (
            self._normalize_summary(
                new_summary
            )
        )

        if not existing_note:

            return bool(
                normalized_new
            )

        existing_latest = (
            self._extract_latest_summary(
                existing_note.note
            )
        )

        return (
            self._normalize_summary(
                existing_latest
            )
            != normalized_new
        )

    # ========================================================
    # PERSIST / APPEND
    # ========================================================

    @transaction.atomic
    def append_summary(
        self,
        *,
        lead: Lead,
        summary: str,
        model: str,
        created_by=None,
    ) -> LeadNote | None:
        """
        Append a new qualification assessment to the existing
        cumulative AI system note.

        Returns:

            LeadNote
                when a meaningful update was appended.

            None
                when the new qualification result is unchanged.
        """

        summary = (
            self._normalize_summary(
                summary
            )
        )

        if not summary:

            raise QualificationError(
                "Qualification summary cannot be empty."
            )

        existing_note = (
            LeadNote.objects
            .select_for_update()
            .filter(
                lead=lead,
                note_type="system",
            )
            .filter(
                note__startswith=(
                    self.QUALIFICATION_HEADER_PREFIX
                )
            )
            .order_by(
                "-created_at",
                "-id",
            )
            .first()
        )

        if not self.has_meaningful_change(
            existing_note=existing_note,
            new_summary=summary,
        ):

            return None

        now = timezone.localtime(
            timezone.now()
        )

        # ----------------------------------------------------
        # INITIAL AI QUALIFICATION NOTE
        # ----------------------------------------------------

        if existing_note is None:

            initial_header = (
                f"<{self.QUALIFICATION_HEADER}"
                f" - "
                f"{now.strftime('%d %b %Y, %I:%M %p')}"
                ">"
            )

            note_text = (
                initial_header
                + "\n"
                + summary
            )

            return LeadNote.objects.create(
                lead=lead,
                created_by=created_by,
                note=note_text,
                note_type="system",
            )

        # ----------------------------------------------------
        # UPDATED AI QUALIFICATION NOTE
        # ----------------------------------------------------

        update_header = (
            "***** Updated Summary "
            f"{now.strftime('%d/%m %H:%M')} *****"
        )

        existing_note.note = (
            existing_note.note.rstrip()
            + "\n\n"
            + update_header
            + "\n\n"
            + summary
        )

        existing_note.save(
            update_fields=[
                "note",
                "updated_at",
            ]
        )

        return existing_note

    # ========================================================
    # FULL WORKFLOW
    # ========================================================

    def generate_and_append(
        self,
        *,
        organization,
        lead: Lead,
        created_by=None,
    ) -> LeadNote | None:
        """
        Full qualification workflow:

            organization + CRM + conversation
                        ↓
               current conversation summary
                        ↓
                    AI analysis
                        ↓
                meaningful-change check
                        ↓
                 system LeadNote
        """

        if organization is None:

            raise QualificationError(
                "Organization is required."
            )

        if lead is None:

            raise QualificationError(
                "Lead is required."
            )

        if lead.organization_id != organization.id:

            raise QualificationError(
                "Lead does not belong to this organization."
            )

        result = self.generate(
            organization=organization,
            lead=lead,
        )

        return self.append_summary(
            lead=lead,
            summary=result.summary,
            model=result.model,
            created_by=created_by,
        )