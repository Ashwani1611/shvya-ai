from __future__ import annotations

import sys
from typing import Any


_INSTALLED = False


def install_qualification_grounding_scope_guard() -> None:
    """Keep qualification grounding bypass scoped to an active authored flow.

    Qualification-specific grounding approval/recovery is valid only while the
    current turn actually carries authored qualification requirements. Normal
    business-fact replies must remain on the standard hallucination/grounding
    path and must never inherit a qualification-specific fast approval.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_answer_routing_runtime as runtime

    original_latest = runtime._latest_persisted_answer

    def scoped_latest_persisted_answer(state: dict[str, Any]):
        if not (state.get("requirements") or []):
            return None
        return original_latest(state)

    runtime._latest_persisted_answer = scoped_latest_persisted_answer

    from apps.ai_engagement.graph import evidence as evidence_module

    original_grounding = evidence_module.check_grounding

    def scoped_grounding(state):
        result = original_grounding(state)
        if state.get("requirements") or not isinstance(result, dict):
            return result

        qualification_bypass = bool(
            result.get("qualification_answer_authoritative")
            or result.get("grounding_recovered")
        )
        if not qualification_bypass:
            return result

        decision = state.get("decision")
        if decision is None:
            return {"grounding_approved": False}
        return {
            "decision": evidence_module._safe_unknown_decision(decision),
            "grounding_approved": False,
        }

    evidence_module.check_grounding = scoped_grounding

    workflow_module = sys.modules.get("apps.ai_engagement.graph.workflow")
    if workflow_module is not None:
        workflow_module.check_grounding = scoped_grounding
        workflow_module.ENGAGEMENT_GRAPH = workflow_module.build_engagement_graph()

    _INSTALLED = True
