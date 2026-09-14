from __future__ import annotations

from copy import deepcopy


_INSTALLED = False


def _latest_inbound_text(lead) -> str:
    try:
        from apps.channels.models import WhatsAppMessage

        message = (
            lead.whatsapp_messages.filter(
                organization_id=lead.organization_id,
                direction=WhatsAppMessage.Direction.INBOUND,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        return str(getattr(message, "body", "") or "").strip()
    except Exception:
        return ""


def _destination_for_action(*, organization, action):
    shift = action.get("stage_shift") if isinstance(action, dict) else None
    stage_id = str((shift or {}).get("stage_id") or "").strip()
    if not stage_id:
        return None
    from apps.crm.models import Stage

    return (
        Stage.objects.select_related("pipeline")
        .filter(
            id=stage_id,
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        )
        .first()
    )


def _qualified_allowed(lead) -> bool:
    from apps.ai_engagement.services.qualification_state import state_for_lead

    state = state_for_lead(lead)
    return str(state.get("qualification_status") or "").casefold() == "completed"


def _nonqualified_evidence_matches(*, organization, destination, latest_text: str) -> bool:
    if not latest_text:
        # Direct/service-level executor callers do not necessarily represent an
        # inbound AI turn. Keep the executor's historical contract unchanged.
        return True

    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement_instruction_policy import (
        compile_engagement_instruction_policy,
    )
    from apps.ai_engagement.services.engagement_instruction_runtime import (
        _condition_part,
        _stage_rule_references_destination,
        _strong_evidence_match,
    )

    destination_payload = {
        "id": str(destination.id),
        "name": destination.name,
        "description": destination.description,
        "pipeline_id": str(destination.pipeline_id),
        "pipeline_name": destination.pipeline.name,
    }
    org_info = OrgInfo.objects.filter(organization=organization).first()
    policy = compile_engagement_instruction_policy(
        getattr(org_info, "engagement_instructions", "") if org_info else ""
    )
    rules = policy.get("stage_shifting") or []
    for rule in rules:
        if not _stage_rule_references_destination(rule, destination_payload):
            continue
        condition = _condition_part(rule, destination_payload)
        if condition and _strong_evidence_match(latest_text, condition):
            return True

    description = str(destination.description or "").strip()
    if description and _strong_evidence_match(latest_text, description):
        return True

    # Explicit destination intent is safe when the lead themselves names the
    # configured destination. This preserves cases such as "I am a seller" for
    # a Seller stage even when an admin has not authored a description yet.
    from apps.ai_engagement.services.engagement_instruction_runtime import _tokens

    stage_tokens = _tokens(destination.name)
    latest_tokens = _tokens(latest_text)
    return bool(stage_tokens and stage_tokens.issubset(latest_tokens))


def _filter_stage_actions(*, organization, lead, actions):
    filtered = []
    latest_text = _latest_inbound_text(lead)
    for action in actions or []:
        if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
            filtered.append(deepcopy(action))
            continue
        destination = _destination_for_action(
            organization=organization,
            action=action,
        )
        if destination is None:
            # Let the existing executor raise its organization/id validation
            # error rather than silently converting an invalid id to success.
            filtered.append(deepcopy(action))
            continue

        # This guard exists for live inbound AI turns. The CRM executor is also
        # a public deterministic service used directly by tests/admin/backend
        # callers, which may have no inbound conversation at all. Do not change
        # those callers' long-standing semantics.
        if not latest_text:
            filtered.append(deepcopy(action))
            continue

        if str(destination.name or "").strip().casefold() == "qualified":
            if _qualified_allowed(lead):
                filtered.append(deepcopy(action))
            continue
        if _nonqualified_evidence_matches(
            organization=organization,
            destination=destination,
            latest_text=latest_text,
        ):
            filtered.append(deepcopy(action))
    return filtered


def install_stage_transition_evidence() -> None:
    """Make the CRM executor the final evidence gate for inbound AI stage moves.

    Keep the executor method signature and direct-service behavior compatible;
    filtering only becomes strict when the lead actually has inbound evidence.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.crm_executor import CRMActionExecutor

    current_execute = CRMActionExecutor.execute

    def execute(self, *, organization, lead, actions, actor=None):
        filtered = _filter_stage_actions(
            organization=organization,
            lead=lead,
            actions=actions,
        )
        return current_execute(
            self,
            organization=organization,
            lead=lead,
            actions=filtered,
            actor=actor,
        )

    CRMActionExecutor.execute = execute
    _INSTALLED = True
