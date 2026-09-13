from __future__ import annotations


_INSTALLED = False


def _normalize(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _explicitly_proposed_qualified(*, decision, context) -> bool:
    pipeline = getattr(context, "pipeline", None)
    if not isinstance(pipeline, dict):
        return False
    by_id = {
        str(item.get("id")): item
        for item in pipeline.get("available_stages") or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    for action in getattr(decision, "crm_actions", []) or []:
        if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
            continue
        shift = action.get("stage_shift")
        stage_id = str(shift.get("stage_id") or "").strip() if isinstance(shift, dict) else ""
        destination = by_id.get(stage_id)
        if destination and _normalize(destination.get("name")) == "qualified":
            return True
    return False


def _guard(current_builder):
    def build(*, decision, context, runtime_policy, qualification_state, requirements):
        controlled, result = current_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )

        if not _explicitly_proposed_qualified(decision=decision, context=context):
            return controlled, result

        stage = getattr(context, "stage", None)
        stage_name = _normalize(stage.get("name") if isinstance(stage, dict) else "")
        projected = result.get("projected_qualification_state") if isinstance(result, dict) else None
        if not isinstance(projected, dict):
            projected = qualification_state if isinstance(qualification_state, dict) else {}

        deterministic_completion = (
            stage_name in {"new lead", "new leads"}
            and str(projected.get("qualification_status") or "").casefold() == "completed"
            and bool(projected.get("all_requirements_answered"))
        )
        if deterministic_completion:
            return controlled, result

        # A model request for Qualified is never a generic stage-routing signal.
        # If deterministic completion did not authorize it, do not allow another
        # description/rule fallback to turn the same turn into a different stage
        # movement either. This preserves Qualified as backend-only authority.
        filtered = [
            item for item in controlled if item.get("type") != "pipeline_transition"
        ]
        return filtered, result

    return build


def install_qualified_transition_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.graph import policy_actions as policy_actions_module

    policy_actions_module.build_controlled_actions = _guard(
        policy_actions_module.build_controlled_actions
    )
    _INSTALLED = True
