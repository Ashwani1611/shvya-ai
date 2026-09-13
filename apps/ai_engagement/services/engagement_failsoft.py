"""Deterministic last-resort customer reply when model validation cannot recover.

The normal engagement path remains authoritative and is always attempted first.
This module exists only to prevent a genuine production WhatsApp turn from
ending in silence because both the primary model result and its schema-repair
result failed validation. It never invents CRM actions, qualification answers,
business facts, pipeline movement, attributes, reminders, or identifiers.
"""

from __future__ import annotations

import logging


_INSTALLED = False
logger = logging.getLogger(__name__)


def build_deterministic_fallback_decision(*, organization, lead):
    """Build a provider-free, RAG-free response from persisted backend state.

    This is intentionally independent from AIContextBuilder. A generation failure
    must not be followed by another model/RAG/context path that can fail for the
    same reason. Qualification fallback uses only the exact authored question
    compiled from OrgInfo; conversation fallback is a neutral acknowledgement.
    """
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement import EngagementDecision
    from apps.ai_engagement.services.organization_profile import (
        compile_qualification_requirements,
    )
    from apps.ai_engagement.services.qualification_state import (
        MODE_QUALIFICATION,
        next_requirement,
        requirements_for_lead,
        state_for_lead,
    )
    from apps.ai_engagement.services.runtime_state import STATE_KEY, observe_message

    org_info = (
        OrgInfo.objects.filter(organization_id=organization.pk)
        .only("qualification_requirements")
        .first()
    )
    compiled = compile_qualification_requirements(
        org_info.qualification_requirements if org_info else ""
    )
    requirements = requirements_for_lead(
        lead,
        compiled.get("requirements", []),
    )
    state = state_for_lead(lead, requirements=requirements)

    attributes = lead.attributes if isinstance(getattr(lead, "attributes", None), dict) else {}
    runtime = attributes.get(STATE_KEY, {})
    latest_inbound = (
        lead.whatsapp_messages.filter(
            organization_id=organization.pk,
            direction="inbound",
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if latest_inbound is not None:
        runtime = observe_message(runtime, latest_inbound.body)

    # Explicit pause/opt-out remains authoritative even if generation failed.
    conversation_mode = runtime.get("conversation_mode")
    if conversation_mode in {"paused", "opt_out"}:
        reason = "OPT_OUT" if conversation_mode == "opt_out" else "NO_ACTION"
        return EngagementDecision(
            should_engage=False,
            message="",
            file_document_id=None,
            crm_actions=[],
            reason=reason,
            reason_code=reason,
            model="deterministic-fallback",
        )

    item = next_requirement(requirements, state.get("requirement_states", {}))
    if (
        state.get("engagement_mode") == MODE_QUALIFICATION
        and state.get("qualification_status") != "completed"
        and isinstance(item, dict)
        and str(item.get("question") or "").strip()
    ):
        # Use the exact backend-compiled question. It already contains authored
        # options in order and does not reconstruct or invent questionnaire text.
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
    # responsive while all CRM side effects are deliberately suppressed.
    return EngagementDecision(
        should_engage=True,
        message="Thanks for your message. I’ve received it. Please share the specific detail you’d like help with.",
        file_document_id=None,
        crm_actions=[],
        reason="NORMAL_CONVERSATION",
        reason_code="NORMAL_CONVERSATION",
        model="deterministic-fallback",
    )


def _fallback_decision(*, service, organization, lead):
    """Compatibility wrapper for the installed EngagementService guard."""
    return build_deterministic_fallback_decision(
        organization=organization,
        lead=lead,
    )


def install_engagement_failsoft() -> None:
    """Wrap the production provider path with a safe last-resort reply.

    Tests and explicit service callers frequently inject a provider specifically
    to validate strict schema/security failures. Those calls must continue to
    raise EngagementError. Production workers instantiate EngagementService
    without an injected provider, so only that path receives fail-soft behavior.
    The worker also owns a second terminal guard so this wrapper is defense in
    depth rather than the only protection against customer-facing silence.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import engagement as engagement_module
    from apps.ai_engagement.services.ai_provider import AIProviderTransientError

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
            # Explicit/injected providers are used by callers that need strict
            # validation semantics. Do not convert their failures into replies.
            if self.provider is not None:
                raise
            # Celery remains the single retry owner for temporary provider/network
            # faults. Fail-soft is for permanent provider/schema/validation cases.
            if isinstance(exc.__cause__, AIProviderTransientError):
                raise
            logger.exception(
                "AI engagement validation/provider path failed for lead %s; using deterministic fail-soft reply",
                getattr(lead, "pk", None),
            )
            return build_deterministic_fallback_decision(
                organization=organization,
                lead=lead,
            )

    service_class.engage = engage
    _INSTALLED = True
