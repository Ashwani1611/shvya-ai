from __future__ import annotations

from dataclasses import dataclass
import json
import re

from apps.ai_engagement.prompts import QUALIFICATION_SUMMARY_INSTRUCTIONS
from apps.ai_engagement.services.summary_limits import compact, merge_summary

from django.db import transaction

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

    QUALIFICATION_INSTRUCTIONS = QUALIFICATION_SUMMARY_INSTRUCTIONS

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

        from apps.ai_engagement.services.internal_summary import InternalSummaryService
        from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
        from apps.ai_engagement.services.qualification_state import state_for_lead
        messages = InternalSummaryService().get_messages(organization=organization, lead=lead)
        profile = compile_org_ai_profile_from_context({
            "qualification_requirements": AIContextBuilder()._build_organization_context(
                organization=organization).get("qualification_requirements", ""),
        })
        requirements = profile.get("qualification", {}).get("requirements", [])
        return json.dumps({
            "requirements": requirements,
            "qualification_state": state_for_lead(lead, requirements=requirements),
            "messages": [{"id": str(m.id), "direction": m.direction, "body": m.body or ""}
                         for m in messages],
        }, ensure_ascii=False)

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

        try:
            source = json.loads(provider_input)
            payload = json.loads(result.text)
            requirements = {str(r["id"]): r for r in source["requirements"]}
            messages = {m["id"]: m for m in source["messages"]}
            facts = payload["answers"]
            if not isinstance(facts, list):
                raise ValueError("answers must be a list")
            rendered = []
            seen = set()
            for fact in facts:
                requirement_id = str(fact["requirement_id"])
                requirement = requirements[requirement_id]
                message = messages[str(fact["message_id"])]
                quote = fact["quote"]
                if (not isinstance(quote, str) or not quote.strip()
                    or message["direction"] != "inbound" or quote not in message["body"]):
                    raise ValueError("Qualification answer lacks inbound evidence")
                # Short answers need the actual preceding question, not a model's guess.
                ordered = source["messages"]
                index = ordered.index(message)
                question = fact["question_quote"]
                if (index == 0 or ordered[index - 1]["direction"] != "outbound"
                    or not isinstance(question, str) or not question.strip()
                    or question not in ordered[index - 1]["body"]):
                    raise ValueError("Qualification answer lacks its preceding question")
                normalize_question = lambda value: re.sub(r"\W+", " ", str(value).casefold()).strip()
                configured_question = normalize_question(requirement.get("question", ""))
                tracked = (source.get("qualification_state", {}).get("requirement_states", {})
                           .get(requirement_id, {}))
                if not (configured_question and configured_question in normalize_question(question)):
                    if str(tracked.get("source_message_id") or "") != message["id"]:
                        raise ValueError("Question is not bound to the configured requirement")
                if requirement_id not in seen:
                    rendered.append(f"{requirement.get('label') or requirement['question']}: {quote.strip()}")
                    seen.add(requirement_id)
            summary = compact("; ".join(rendered))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise QualificationError("Invalid qualification evidence; no note saved.") from exc
        return QualificationResult(summary=summary, model=result.model)

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

        Lead.objects.select_for_update().get(pk=lead.pk)
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

        # ----------------------------------------------------
        # INITIAL AI QUALIFICATION NOTE
        # ----------------------------------------------------

        if existing_note is None:

            initial_header = f"<{self.QUALIFICATION_HEADER} - Evidence>"

            note_text = (
                initial_header
                + "\n"
                + compact(summary, 500 - len(initial_header) - 1)
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

        header = f"<{self.QUALIFICATION_HEADER} - Evidence>"
        previous = (self._extract_latest_summary(existing_note.note)
                    if existing_note.note.startswith(header) else "")
        additions = "; ".join(part for part in summary.split("; ")
                              if part.casefold() not in previous.casefold())
        if not additions:
            return None
        updated_text = header + "\n" + merge_summary(
            previous, additions, limit=500 - len(header) - 1,
        )
        if updated_text == existing_note.note:
            return None
        existing_note.note = updated_text

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

        if not result.summary:
            return None

        return self.append_summary(
            lead=lead,
            summary=result.summary,
            model=result.model,
            created_by=created_by,
        )
