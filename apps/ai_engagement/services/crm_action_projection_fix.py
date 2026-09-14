from __future__ import annotations


_INSTALLED = False


def _unique_organization_qualified_stage(lead):
    """Prefer current-pipeline Qualified; use cross-pipeline only when unambiguous."""
    from apps.ai_engagement.services.qualification_state import normalize_stage_name

    pipeline = getattr(lead, "pipeline", None)
    if pipeline is not None:
        current = list(
            pipeline.stages.filter(is_active=True).order_by("display_order", "name", "id")
        )
        for stage in current:
            if normalize_stage_name(stage.name) == "qualified":
                return stage

    organization = getattr(lead, "organization", None)
    if organization is None:
        return None

    # The AI Sandbox uses an in-memory lead/organization so it can exercise the
    # production qualification runtime without creating CRM rows. Cross-pipeline
    # stage lookup only makes sense for a real Django model instance; passing the
    # sandbox namespace into a UUID foreign-key lookup raises ValidationError.
    if getattr(organization, "_meta", None) is None:
        return None

    from apps.crm.models import Stage

    candidates = list(
        Stage.objects.select_related("pipeline")
        .filter(
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        )
        .exclude(pipeline_id=getattr(lead, "pipeline_id", None))
        .order_by("pipeline__name", "display_order", "name", "id")
    )
    candidates = [stage for stage in candidates if normalize_stage_name(stage.name) == "qualified"]
    return candidates[0] if len(candidates) == 1 else None


def _projected_action_builder(current_builder):
    def build(*, decision, context, runtime_policy, qualification_state, requirements):
        controlled, result = current_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )

        stage = getattr(context, "stage", None)
        stage_name = ""
        if isinstance(stage, dict):
            stage_name = str(stage.get("name") or "").strip().casefold()
        if stage_name == "new leads":
            stage_name = "new lead"
        if stage_name != "new lead":
            return controlled, result

        # The model can capture a valid natural-language qualification answer on
        # the current turn even when deterministic extraction did not persist it
        # before action planning. Project that already-validated answer now so
        # attribute writes and final qualification completion use one state.
        from apps.ai_engagement.services.qualification_state import project_answer_updates

        messages = (getattr(context, "conversation", None) or {}).get("messages", [])
        projected = project_answer_updates(
            state=qualification_state,
            requirements=requirements,
            updates=getattr(decision, "qualification_updates", []) or [],
            messages=messages,
        )

        from apps.ai_engagement.services.crm_routing_reliability import (
            _ensure_qualified_transition,
            _latest_inbound,
            _merge_attribute_updates,
        )

        latest_message_id, _latest_text = _latest_inbound(context)
        _merge_attribute_updates(
            controlled,
            qualification_state=projected,
            requirements=requirements,
            context=context,
            latest_message_id=latest_message_id,
        )
        _ensure_qualified_transition(
            controlled,
            context=context,
            qualification_state=projected,
            requirements=requirements,
        )
        return controlled, {**result, "projected_qualification_state": projected}

    return build


def install_crm_action_projection_fix() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_state as qualification_state_module

    qualification_state_module._qualified_stage = _unique_organization_qualified_stage

    from apps.ai_engagement.graph import policy_actions as policy_actions_module

    policy_actions_module.build_controlled_actions = _projected_action_builder(
        policy_actions_module.build_controlled_actions
    )
    _INSTALLED = True
