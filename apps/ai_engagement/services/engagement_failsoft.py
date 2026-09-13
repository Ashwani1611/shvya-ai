"""Deterministic last-resort customer reply when model validation cannot recover.

The normal engagement path remains authoritative and is always attempted first.
This wrapper exists only to prevent a genuine inbound WhatsApp turn from ending
in silence because both the primary model result and its schema-repair result
failed validation. It never invents CRM actions, qualification answers, business
facts, pipeline movement, attributes, reminders, or identifiers.
"""

from __future__ import annotations

import logging


_INSTALLED = False
logger = logging.getLogger(__name__)


def _fallback_decision(*, service, organization, lead):
    from apps.ai_engagement.services.engagement import EngagementDecision
    from apps.ai_engagement.services.organization_profile import (
        compile_org_ai_profile_from_context,
    )
    from apps.ai_engagement.services.qualification_state import (
        MODE_QUALIFICATION,
        next_requirement,
        requirements_for_lead,
        state_for_lead,
    )
    from apps.ai_engagement.services.runtime_state import STATE_KEY

    context = service.context_builder.build(
        organization=organization,
        lead=lead,
        knowledge_query=None,
        message_limit=service.MESSAGE_LIMIT,
        knowledge_limit=service.KNOWLEDGE_LIMIT,
        note_limit=service.NOTE_LIMIT,
    )
    profile = compile_org_ai_profile_from_context(context.organization or {})
    requirements = requirements_for_lead(
        lead,
        profile.get("qualification", {}).get("requirements", []),
    )
    state = state_for_lead(lead, requirements=requirements)
    runtime = (lead.attributes or {}).get(STATE_KEY, {}) if isinstance(lead.attributes, dict) else {}

    # An explicit opt-out remains a hard stop even when the provider/validator
    # failed. Never turn fail-soft behavior into an opt-out bypass.
    if runtime.get("conversation_mode") == "opt_out":
        return EngagementDecision(
            should_engage=False,
            message="",
            file_document_id=None,
            crm_actions=[],
            reason="OPT_OUT",
            reason_code="OPT_OUT",
            model="deterministic-fallback",
        )

    item = next_requirement(requirements, state.get("requirement_states", {}))
    if (
        state.get("engagement_mode") == MODE_QUALIFICATION
        and state.get("qualification_status") != "completed"
        and isinstance(item, dict)
        and str(item.get("question") or "").strip()
    ):
        # Use the exact backend-compiled question. It already includes authored
        # options in order, which satisfies runtime validation without asking the
        # model to reconstruct the questionnaire.
        return EngagementDecision(
            should_engage=True,
            message=str(item["question"]).strip(),
            file_document_id=None,
            crm_actions=[],
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            next_requirement_id=str(item.get("id") or "") or None,
            model="deterministic-fallback",
        )

    # Conversation mode cannot safely manufacture organization-specific facts
    # without a valid model result. A neutral acknowledgement keeps the channel
    # responsive and invites a concrete follow-up while all CRM side effects are
    # deliberately suppressed.
    return EngagementDecision(
        should_engage=True,
        message="Thanks for your message. I’ve received it. Please share the specific detail you’d like help with.",
        file_document_id=None,
        crm_actions=[],
        reason="NORMAL_CONVERSATION",
        reason_code="NORMAL_CONVERSATION",
        model="deterministic-fallback",
    )


def install_engagement_failsoft() -> None:
    """Wrap the fully installed engagement stack with a safe last-resort reply."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import engagement as engagement_module

    service_class = engagement_module.EngagementService
    original_engage = service_class.engage

    def engage(self, *, organization, lead, knowledge_query=None, context=None):
        try:
            return original_engage(
                self,
                organization=organization,
                lead=lead,
                knowledge_query=knowledge_query,
                context=context,
            )
        except engagement_module.EngagementError as exc:
            logger.exception(
                "AI engagement validation/provider path failed for lead %s; using deterministic fail-soft reply",
                getattr(lead, "pk", None),
            )
            return _fallback_decision(
                service=self,
                organization=organization,
                lead=lead,
            )

    service_class.engage = engage
    _INSTALLED = True
