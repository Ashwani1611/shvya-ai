from __future__ import annotations

from dataclasses import replace


_INSTALLED = False


def install_post_state_finalization_guard() -> None:
    """Make the post-commit engagement pass language-only before validation.

    State-changing turns intentionally run engagement twice: the first pass
    proposes mutations, the transactional runtime commits them, and the second
    pass writes the customer-facing reply from committed state. A model can
    still repeat stale CRM/qualification proposals in that second structured
    response, so neutralize those proposals immediately after normalization and
    before qualification validation/repair runs.

    The authoritative file selection is applied later by
    ``transactional_decision_reuse.engage_from_committed_state`` from persisted
    runtime state; this guard only prevents already-resolved mutations from
    being re-validated or re-executed.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.engagement import EngagementService

    current_normalize = EngagementService._normalize_result

    def normalize_post_state_result(self, *, result):
        decision = current_normalize(self, result=result)
        cached = runtime._PRECOMPUTED_DECISION.get()
        if not isinstance(cached, dict) or not cached.get("force_regenerate"):
            return decision
        return replace(
            decision,
            crm_actions=[],
            qualification_updates=[],
        )

    EngagementService._normalize_result = normalize_post_state_result
    _INSTALLED = True
