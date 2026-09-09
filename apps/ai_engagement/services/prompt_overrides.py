from __future__ import annotations

import json


_INSTALLED = False


def install_fixed_prompt_overrides() -> None:
    """Attach version-controlled prompt behavior to existing services.

    The service classes keep their public APIs, so existing tasks/views/tests do
    not need a framework migration. The conversation summary gains the uploaded
    prompt's rolling-summary contract while remaining backward compatible with
    legacy plain-prose provider outputs.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.prompts import (
        INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS,
        QUALIFICATION_SUMMARY_INSTRUCTIONS,
    )
    from apps.ai_engagement.services.internal_summary import InternalSummaryService
    from apps.ai_engagement.services.qualification import QualificationService

    InternalSummaryService.SUMMARY_INSTRUCTIONS = (
        INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS
    )
    InternalSummaryService.DEFAULT_MESSAGE_LIMIT = 24
    QualificationService.QUALIFICATION_INSTRUCTIONS = (
        QUALIFICATION_SUMMARY_INSTRUCTIONS
    )

    original_build_input = InternalSummaryService.build_provider_input
    original_generate = InternalSummaryService.generate_summary

    def rolling_build_provider_input(self, *, organization, lead, messages):
        base = original_build_input(
            self,
            organization=organization,
            lead=lead,
            messages=messages,
        )
        current = self.get_current_summary(
            organization=organization,
            lead=lead,
        )
        existing = (current.summary or "").strip() if current else ""
        return (
            "EXISTING CONVERSATION SUMMARY\n"
            f"{existing or 'Empty'}\n\n"
            "RECENT CONTEXT TO MERGE\n"
            f"{base}"
        )

    def json_aware_generate_summary(self, *, organization, lead, messages):
        summary, model = original_generate(
            self,
            organization=organization,
            lead=lead,
            messages=messages,
        )
        text = (summary or "").strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("summary"), str):
                normalized = payload["summary"].strip()
                if normalized:
                    text = normalized
        # Enforce the hard ceiling even if a provider ignores the prompt.
        text = text[:700].strip()
        return text, model

    InternalSummaryService.build_provider_input = rolling_build_provider_input
    InternalSummaryService.generate_summary = json_aware_generate_summary

    _INSTALLED = True
