"""Prevent the language model from authorizing customer-facing silence.

Reply eligibility is an application decision. The LLM may decide wording,
extraction and proposals, but it cannot suppress an otherwise eligible inbound
message. Explicit opt-out is resolved deterministically before generation; AI
permission, transport, duplicate and superseded-turn checks remain owned by the
existing backend/task layers.
"""

from __future__ import annotations

import logging


_INSTALLED = False
logger = logging.getLogger(__name__)


def _latest_inbound_text(*, service, organization, lead, context=None) -> str:
    if context is not None:
        try:
            return str(service._latest_inbound_text(context=context) or "").strip()
        except Exception:
            logger.debug("Unable to read latest inbound from supplied AI context", exc_info=True)

    resolver = getattr(service.context_builder, "latest_inbound_for_fallback", None)
    if callable(resolver):
        try:
            message = resolver(organization=organization, lead=lead)
            return str(getattr(message, "body", "") or "").strip()
        except Exception:
            logger.debug("Unable to resolve latest inbound from context builder", exc_info=True)

    manager = getattr(lead, "whatsapp_messages", None)
    if manager is not None:
        try:
            message = (
                manager.filter(
                    organization=organization,
                    direction="inbound",
                )
                .order_by("-created_at", "-id")
                .first()
            )
            return str(getattr(message, "body", "") or "").strip()
        except Exception:
            logger.debug("Unable to resolve latest inbound from WhatsApp relation", exc_info=True)

    return ""


def _runtime_is_opted_out(*, lead, latest_text: str) -> bool:
    from apps.ai_engagement.services.runtime_state import (
        STATE_KEY,
        is_explicit_opt_out,
    )

    attributes = (
        lead.attributes
        if isinstance(getattr(lead, "attributes", None), dict)
        else {}
    )
    saved = attributes.get(STATE_KEY)
    if isinstance(saved, dict) and saved.get("conversation_mode") == "opt_out":
        return True
    return is_explicit_opt_out(latest_text)


def _deterministic_opt_out_decision():
    from apps.ai_engagement.services.engagement import EngagementDecision

    return EngagementDecision(
        should_engage=False,
        message="",
        file_document_id=None,
        crm_actions=[],
        reason="OPT_OUT",
        reason_code="OPT_OUT",
        model="deterministic-opt-out",
        qualification_updates=[],
        silence_rule=None,
    )


def install_model_silence_guard() -> None:
    """Make reply/no-reply a backend boundary instead of an LLM choice."""

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import engagement as engagement_module

    service_class = engagement_module.EngagementService
    original_engage = service_class.engage

    def backend_owned_engage(
        self,
        *,
        organization,
        lead,
        knowledge_query=None,
        context=None,
    ):
        latest_text = _latest_inbound_text(
            service=self,
            organization=organization,
            lead=lead,
            context=context,
        )
        if _runtime_is_opted_out(lead=lead, latest_text=latest_text):
            return _deterministic_opt_out_decision()

        return original_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )

    def backend_owned_engagement_policy(self, *, decision, context):
        """Reject model-authored silence so repair/fail-soft must return a reply."""
        if decision.should_engage:
            return

        raise engagement_module.EngagementError(
            "Model-authored silence is not allowed for customer engagement. "
            "Explicit opt-out, disabled AI permissions, transport restrictions, "
            "duplicate delivery and superseded-turn suppression are backend-owned. "
            "Return a grounded customer-facing reply. UNKNOWN_INFORMATION and "
            "HUMAN_HANDOFF must still respond without inventing facts or claiming "
            "an unconfirmed action."
        )

    service_class.engage = backend_owned_engage
    service_class._validate_engagement_policy = backend_owned_engagement_policy
    service_class._model_silence_guard_original_engage = original_engage

    _INSTALLED = True
