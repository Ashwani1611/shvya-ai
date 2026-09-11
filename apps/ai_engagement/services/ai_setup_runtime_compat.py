from __future__ import annotations

from types import SimpleNamespace


_INSTALLED = False


def install_ai_setup_runtime_compat() -> None:
    """Keep AI Setup action validation compatible with bounded test/caller contexts.

    Production AIContext always carries a stage object. Some internal callers and
    unit tests intentionally construct a smaller context containing only the
    conversation and pipeline. Stage-transition validation must remain optional
    in those paths instead of breaking unrelated CRM actions.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.graph import policy_actions as policy_actions_module

    current_builder = policy_actions_module.build_controlled_actions

    def compatible_builder(
        *,
        decision,
        context,
        runtime_policy,
        qualification_state,
        requirements,
    ):
        safe_context = context
        if not hasattr(context, "stage"):
            values = dict(getattr(context, "__dict__", {}) or {})
            values["stage"] = {}
            safe_context = SimpleNamespace(**values)

        return current_builder(
            decision=decision,
            context=safe_context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )

    policy_actions_module.build_controlled_actions = compatible_builder
    _INSTALLED = True
