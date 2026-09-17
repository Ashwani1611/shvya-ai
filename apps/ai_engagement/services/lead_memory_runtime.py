from __future__ import annotations

import json
import logging
from functools import wraps

from apps.ai_engagement.services.lead_memory import LeadMemoryService


logger = logging.getLogger(__name__)
_INSTALLED = False


def _record_memory_failure(*, stage: str, exc: Exception) -> None:
    try:
        from apps.ai_engagement.services.trace_service import record

        record(
            "memory",
            {
                "status": "unavailable",
                "stage": stage,
                "error_type": exc.__class__.__name__,
            },
        )
    except Exception:
        return


def install_lead_memory_runtime() -> None:
    """Install Phase 6 without changing channel-specific execution semantics.

    The provider-input wrapper adds the three conceptual levels while preserving
    the existing recent conversation and rolling summary fields. The persistence
    wrapper runs only after the shared backend has accepted/finalized the inbound
    turn, so WhatsApp API, Coexistence and Hosted all use the same memory path.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement import tasks as task_module
    from apps.ai_engagement.services.engagement import EngagementService

    current_build_input = EngagementService._build_input

    @wraps(current_build_input)
    def build_input_with_memory(self, *args, **kwargs):
        raw = current_build_input(self, *args, **kwargs)
        context = kwargs.get("context")
        if context is None and args:
            context = args[0]
        if context is None:
            return raw

        organization_id = str((getattr(context, "organization", None) or {}).get("id") or "")
        lead_id = str((getattr(context, "lead", None) or {}).get("id") or "")
        if not organization_id or not lead_id:
            return raw

        try:
            payload = json.loads(raw)
            payload = LeadMemoryService().augment_provider_payload(
                payload=payload,
                organization_id=organization_id,
                lead_id=lead_id,
            )
        except Exception as exc:
            logger.exception(
                "Lead memory context unavailable for organization %s lead %s",
                organization_id,
                lead_id,
            )
            _record_memory_failure(stage="context_read", exc=exc)
            return raw

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    EngagementService._build_input = build_input_with_memory

    current_persist_answers = task_module._persist_engagement_answers

    @wraps(current_persist_answers)
    def persist_answers_with_memory(lead, decision, source_message_id):
        accepted = current_persist_answers(lead, decision, source_message_id)
        if not accepted:
            return accepted

        try:
            LeadMemoryService().update_from_accepted_turn(
                organization=lead.organization,
                lead=lead,
                source_message_id=source_message_id,
            )
        except Exception as exc:
            # Memory is an enrichment layer. A memory outage must not turn an
            # otherwise valid customer reply into a WhatsApp delivery failure.
            logger.exception(
                "Lead memory update failed for organization %s lead %s",
                getattr(lead, "organization_id", None),
                getattr(lead, "id", None),
            )
            _record_memory_failure(stage="accepted_turn_update", exc=exc)

        return accepted

    task_module._persist_engagement_answers = persist_answers_with_memory
    _INSTALLED = True
