from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Any

from apps.ai_engagement.services.intent_engine import (
    ClassificationPath,
    Intent,
    IntentDecision,
    IntentEngine,
)


logger = logging.getLogger(__name__)
_INSTALLED = False
_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar(
    "shvya_intent_decision",
    default=None,
)


def current_intent_decision(*, organization_id=None, lead_id=None, source_message_id=None):
    """Return the current observation only when all supplied scope keys match."""
    scoped = _CURRENT.get()
    if not isinstance(scoped, dict):
        return None
    for key, expected in (
        ("organization_id", organization_id),
        ("lead_id", lead_id),
        ("source_message_id", source_message_id),
    ):
        if expected is not None and str(scoped.get(key) or "") != str(expected):
            return None
    return scoped.get("decision")


def _latest_inbound(context):
    conversation = getattr(context, "conversation", None)
    messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
    for message in reversed(messages or []):
        if isinstance(message, dict) and str(message.get("direction") or "").casefold() == "inbound":
            body = str(message.get("body") or "").strip()
            if body:
                return str(message.get("id") or "") or None, body
    return None, ""


def _fallback(*, direct_question=None, language=None, exc=None):
    return IntentDecision(
        primary_intent=Intent.UNKNOWN,
        confidence=0.0,
        direct_question=direct_question,
        classification_path=ClassificationPath.FALLBACK,
        language=language,
        classification_error=exc.__class__.__name__ if exc is not None else None,
    )


def classify_context(*, organization, lead, context) -> IntentDecision | None:
    """Classify one already tenant-scoped AI context without mutating CRM state."""
    source_message_id, message = _latest_inbound(context)
    if not message:
        return None

    from apps.ai_engagement.services.organization_profile import (
        compile_org_ai_profile_from_context,
    )
    from apps.ai_engagement.services.qualification_state import (
        requirements_for_lead,
        state_for_lead,
    )

    profile = compile_org_ai_profile_from_context(context.organization or {})
    configured = (profile.get("qualification") or {}).get("requirements") or []
    requirements = requirements_for_lead(lead, configured)
    qualification_state = state_for_lead(lead, requirements=requirements)
    return IntentEngine().classify(
        organization=organization,
        lead=lead,
        message=message,
        source_message_id=source_message_id,
        requirements=requirements,
        qualification_state=qualification_state,
        context=context,
    )


def _record_trace(*, decision: IntentDecision, elapsed_ms: int) -> None:
    # Phase 1 trace is deliberately the only persistence surface. trace_service
    # itself is fail-soft; this extra guard ensures trace observation can never
    # become customer-message policy or availability authority.
    try:
        from apps.ai_engagement.services.trace_service import record

        record("intent", {"status": "AVAILABLE", **decision.as_dict()})
        record("performance", {"intent_ms": max(int(elapsed_ms), 0)})
    except Exception:
        logger.exception("Intent trace recording failed; continuing normal AI engagement")


def install_intent_runtime() -> None:
    """Attach the shared Phase 2 Intent Engine to the central context boundary."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from apps.ai_engagement.services import context as context_module

    original_build = context_module.AIContextBuilder.build

    def build_with_intent(self, *args, **kwargs):
        context = original_build(self, *args, **kwargs)

        # Engagement may rebuild the same context after deciding to invoke RAG.
        # Classify only the initial inbound context so one ambiguous turn causes
        # at most one intent-model call.
        if str(kwargs.get("knowledge_query") or "").strip() or kwargs.get("query_vector") is not None:
            return context

        organization = kwargs.get("organization")
        lead = kwargs.get("lead")
        if organization is None or lead is None:
            return context

        # Only live API/Hosted AI turns own a Phase 1 trace. Avoid adding an
        # intent-model call to unrelated context-builder consumers such as
        # summaries, admin utilities, or background enrichment.
        from apps.ai_engagement.services.trace_service import current

        if current() is None:
            return context

        source_message_id, _ = _latest_inbound(context)
        started = time.perf_counter()
        try:
            decision = classify_context(
                organization=organization,
                lead=lead,
                context=context,
            )
        except Exception as exc:
            logger.exception("Intent classification failed; continuing normal AI engagement")
            decision = _fallback(exc=exc)

        if decision is None:
            return context

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _CURRENT.set(
            {
                "organization_id": str(getattr(organization, "id", "") or ""),
                "lead_id": str(getattr(lead, "id", "") or ""),
                "source_message_id": str(source_message_id or ""),
                "decision": decision,
            }
        )
        _record_trace(decision=decision, elapsed_ms=elapsed_ms)
        return context

    context_module.AIContextBuilder.build = build_with_intent
