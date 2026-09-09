from __future__ import annotations

import json


_INSTALLED = False


def install_fixed_prompt_overrides() -> None:
    """Attach version-controlled prompt behavior to existing services.

    Existing public service contracts remain stable while internal prompts and
    compact runtime payloads evolve. Organization values stay available at the
    legacy payload keys, and the structured AI profile carries only the derived
    machine-readable pieces so we do not pay twice for the same long free text.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.prompts import (
        INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS,
        QUALIFICATION_SUMMARY_INSTRUCTIONS,
    )
    from apps.ai_engagement.services.engagement import EngagementService
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
    original_engagement_build_input = EngagementService._build_input

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
        text = text[:700].strip()
        return text, model

    def organization_compatible_engagement_input(self, *, context, **kwargs):
        raw = original_engagement_build_input(
            self,
            context=context,
            **kwargs,
        )
        payload = json.loads(raw)
        organization = payload.get("organization")
        source = context.organization if isinstance(context.organization, dict) else {}
        if isinstance(organization, dict):
            for key in (
                "about",
                "bot_languages",
                "qualification_requirements",
                "engagement_instructions",
                "bump_up_enabled",
                "bump_up_count",
            ):
                organization[key] = source.get(key)

            # The same free text is now present at the long-standing top-level
            # keys. Keep only derived/structured values in ai_profile to avoid
            # duplicating those tokens in every OpenAI request.
            profile = organization.get("ai_profile")
            if isinstance(profile, dict):
                identity = profile.get("identity")
                if isinstance(identity, dict):
                    identity.pop("about", None)
                communication = profile.get("communication")
                if isinstance(communication, dict):
                    communication.pop("custom_instructions", None)
                    communication.pop("languages", None)
                qualification = profile.get("qualification")
                if isinstance(qualification, dict):
                    qualification.pop("raw", None)

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    InternalSummaryService.build_provider_input = rolling_build_provider_input
    InternalSummaryService.generate_summary = json_aware_generate_summary
    EngagementService._build_input = organization_compatible_engagement_input

    _INSTALLED = True
