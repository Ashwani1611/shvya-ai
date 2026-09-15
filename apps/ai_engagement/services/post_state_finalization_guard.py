from __future__ import annotations

from contextvars import ContextVar
from dataclasses import replace


_INSTALLED = False
_FINAL_LANGUAGE_ONLY: ContextVar[bool] = ContextVar(
    "shvya_post_state_final_language_only",
    default=False,
)


def install_post_state_finalization_guard() -> None:
    """Make only the exact post-commit engagement pass language-only.

    State-changing turns intentionally run engagement twice: the first pass
    proposes mutations, the transactional runtime commits them, and the second
    pass writes the customer-facing reply from committed state. A model can
    still repeat stale CRM/qualification proposals in that second structured
    response, so neutralize those proposals immediately after normalization and
    before qualification validation/repair runs.

    The language-only flag is scoped to the exact final regeneration call. It is
    not derived directly from the longer-lived precomputed-decision marker, so a
    stale marker cannot affect unrelated direct, playground, or test generation.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.engagement import EngagementService
    from apps.ai_engagement.services.transactional_decision_reuse import (
        _pending_post_state_turn,
    )

    current_engage = EngagementService.engage

    def engage_with_scoped_finalization(
        self,
        *,
        organization,
        lead,
        knowledge_query=None,
        context=None,
    ):
        pending = _pending_post_state_turn(runtime=runtime, lead=lead)
        if pending is None:
            return current_engage(
                self,
                organization=organization,
                lead=lead,
                knowledge_query=knowledge_query,
                context=context,
            )

        token = _FINAL_LANGUAGE_ONLY.set(True)
        try:
            return current_engage(
                self,
                organization=organization,
                lead=lead,
                knowledge_query=knowledge_query,
                context=context,
            )
        finally:
            _FINAL_LANGUAGE_ONLY.reset(token)

    EngagementService.engage = engage_with_scoped_finalization

    current_normalize = EngagementService._normalize_result

    def normalize_post_state_result(self, *, result):
        decision = current_normalize(self, result=result)
        if not _FINAL_LANGUAGE_ONLY.get():
            return decision
        return replace(
            decision,
            crm_actions=[],
            qualification_updates=[],
        )

    EngagementService._normalize_result = normalize_post_state_result
    _INSTALLED = True
