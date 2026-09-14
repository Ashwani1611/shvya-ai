from __future__ import annotations

from typing import Any


_INSTALLED = False


def install_qualification_grounding_scope_guard() -> None:
    """Keep qualification grounding bypass scoped to an active authored flow.

    The qualification routing runtime may recover a rejected grounding result only
    when the current graph state actually carries qualification requirements. A
    normal business-fact answer with no active requirements must always remain on
    the standard hallucination/grounding path.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_answer_routing_runtime as runtime

    original = runtime._latest_persisted_answer

    def scoped_latest_persisted_answer(state: dict[str, Any]):
        if not (state.get("requirements") or []):
            return None
        return original(state)

    runtime._latest_persisted_answer = scoped_latest_persisted_answer
    _INSTALLED = True
