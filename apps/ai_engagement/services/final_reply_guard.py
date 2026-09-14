"""Final customer-facing reply invariant for EngagementService.

The production task owns permission/transport/duplicate suppression before and
after EngagementService. Inside EngagementService itself, the only intentional
silent customer decision is an explicit deterministic opt-out. A paused turn
(`not now`, `later`, etc.) still receives a brief acknowledgement, and any other
unexpected silent result becomes a fact-free confirmation-needed response.
"""

from __future__ import annotations

from dataclasses import replace


_INSTALLED = False


def _conversation_mode(lead) -> str:
    from apps.ai_engagement.services.runtime_state import STATE_KEY

    attributes = (
        lead.attributes
        if isinstance(getattr(lead, "attributes", None), dict)
        else {}
    )
    state = attributes.get(STATE_KEY)
    if not isinstance(state, dict):
        return ""
    return str(state.get("conversation_mode") or "").strip().casefold()


def install_final_reply_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService

    original_engage = EngagementService.engage

    def engage(self, *, organization, lead, knowledge_query=None, context=None):
        decision = original_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )
        if getattr(decision, "should_engage", False):
            return decision

        reason_code = str(
            getattr(decision, "reason_code", "")
            or getattr(decision, "reason", "")
            or ""
        ).strip().upper()
        if reason_code == "OPT_OUT":
            return decision

        if _conversation_mode(lead) == "paused":
            return replace(
                decision,
                should_engage=True,
                message="No problem. We can continue whenever you're ready.",
                reason="NORMAL_CONVERSATION",
                reason_code="NORMAL_CONVERSATION",
                silence_rule=None,
                crm_actions=[],
                qualification_updates=[],
                next_requirement_id=None,
                file_document_id=None,
                model="deterministic-paused-ack",
            )

        # This is deliberately fact-free. Reaching this branch means a lower
        # layer produced silence even though no deterministic opt-out exists.
        # Never invent a business answer merely to avoid silence.
        return replace(
            decision,
            should_engage=True,
            message=(
                "I don’t have enough verified information to answer that "
                "confidently. The team would need to confirm it."
            ),
            reason="UNKNOWN_INFORMATION",
            reason_code="UNKNOWN_INFORMATION",
            silence_rule=None,
            crm_actions=[],
            qualification_updates=[],
            next_requirement_id=None,
            file_document_id=None,
            model="deterministic-reply-guard",
        )

    EngagementService.engage = engage
    _INSTALLED = True
