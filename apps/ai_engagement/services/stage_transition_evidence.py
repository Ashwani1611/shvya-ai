from __future__ import annotations

from copy import deepcopy
from functools import wraps


_INSTALLED = False


def _latest_inbound_text(lead, *, source_message=None) -> str:
    if source_message is not None:
        return str(source_message.body or "").strip()
    try:
        from apps.channels.models import WhatsAppMessage

        message = (
            lead.whatsapp_messages.filter(
                organization_id=lead.organization_id,
                direction=WhatsAppMessage.Direction.INBOUND,
            ).order_by("-created_at", "-id").first()
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

    return Stage.objects.select_related("pipeline").filter(
        id=stage_id, pipeline__organization=organization,
        pipeline__is_active=True, is_active=True,
    ).first()


def _configured_completion_allowed(*, organization, lead, destination) -> bool:
    """Authorize only the configured completion target from verified state."""
    try:
        from apps.ai_engagement.services import qualification_state as qs
        from apps.ai_engagement.services.qualification_execution_contract import _config
        from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn

        requirements = _requirements_for_turn(organization=organization, lead=lead)
        if not requirements:
            return False
        state = qs.state_for_lead(lead, requirements=requirements)
        if str(state.get("qualification_status") or "").casefold() != "completed":
            return False
        config = _config(organization=organization, requirements=requirements)
        target = config.get("completion_stage")
        return bool(isinstance(target, dict) and str(target.get("id") or "") == str(destination.id))
    except Exception:
        return False


def _nonqualified_evidence_matches(*, organization, destination, latest_text: str) -> bool:
    if not latest_text:
        return False
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement_instruction_policy import compile_engagement_instruction_policy
    from apps.ai_engagement.services.engagement_instruction_runtime import (
        _condition_part, _stage_rule_references_destination, _strong_evidence_match,
    )

    destination_payload = {
        "id": str(destination.id), "name": destination.name,
        "description": destination.description, "pipeline_id": str(destination.pipeline_id),
        "pipeline_name": destination.pipeline.name,
    }
    org_info = OrgInfo.objects.filter(organization=organization).first()
    policy = compile_engagement_instruction_policy(
        getattr(org_info, "engagement_instructions", "") if org_info else ""
    )
    for rule in policy.get("stage_shifting") or []:
        if not _stage_rule_references_destination(rule, destination_payload):
            continue
        condition = _condition_part(rule, destination_payload)
        if condition and _strong_evidence_match(latest_text, condition):
            return True
    description = str(destination.description or "").strip()
    if description and _strong_evidence_match(latest_text, description):
        return True
    from apps.ai_engagement.services.engagement_instruction_runtime import _tokens

    stage_tokens = _tokens(destination.name)
    latest_tokens = _tokens(latest_text)
    return bool(stage_tokens and stage_tokens.issubset(latest_tokens))


def _filter_stage_actions(*, organization, lead, actions, source_message=None):
    filtered = []
    latest_text = _latest_inbound_text(lead, source_message=source_message)
    for action in actions or []:
        if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
            filtered.append(deepcopy(action))
            continue
        destination = _destination_for_action(organization=organization, action=action)
        if destination is None:
            filtered.append(deepcopy(action))
            continue
        if _configured_completion_allowed(organization=organization, lead=lead, destination=destination):
            filtered.append(deepcopy(action))
            continue
        if _nonqualified_evidence_matches(
            organization=organization, destination=destination, latest_text=latest_text,
        ):
            filtered.append(deepcopy(action))
    return filtered


def _inside_ai_turn(*, organization) -> bool:
    """Use the existing in-memory execution marker, never trace DB availability."""
    try:
        from apps.ai_engagement.services.trace_service import current

        trace = current()
    except Exception:
        return False
    return bool(trace is not None and str(trace.organization_id) == str(organization.id))


def _bound_source(*, organization, lead, supplied=None):
    """Resolve the exact accepted inbound, not a newer message on another account."""
    from apps.ai_engagement.services.crm_executor import CRMActionExecutionError
    from apps.ai_engagement.services.tenant_guard import TenantGuard, TenantScopeError
    from apps.ai_engagement.services.trace_service import current
    from apps.channels.models import WhatsAppMessage
    from django.core.exceptions import ValidationError

    source = supplied
    if source is None:
        trace = current()
        source_id = (
            (trace.data.get("input") or {}).get("source_inbound_message_id")
            if trace is not None and str(trace.organization_id) == str(organization.id)
            else None
        )
        if not source_id:
            from apps.ai_engagement.services.phase7_completion_runtime import _policy_turn

            turn = _policy_turn(organization=organization, lead=lead)
            source_id = (turn or {}).get("source_message_id")
        if not source_id:
            return None
        try:
            source = WhatsAppMessage.objects.select_related("account", "lead").filter(
                pk=source_id, organization=organization, lead=lead, direction="inbound",
            ).first()
        except (ValueError, ValidationError) as exc:
            raise CRMActionExecutionError("Invalid AI action source message.") from exc
        if source is None:
            raise CRMActionExecutionError("AI action source is outside organization/lead scope.")
    try:
        TenantGuard(organization).validate_message(source, lead=lead)
    except TenantScopeError as exc:
        raise CRMActionExecutionError(exc.code) from exc
    if source.direction != "inbound" or source.lead_id != lead.id:
        raise CRMActionExecutionError("AI actions require this lead's inbound message.")
    return source


def install_stage_transition_evidence() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor

    current_execute = CRMActionExecutor.execute

    @wraps(current_execute)
    def execute(self, *, organization, lead, actions, actor=None, source_message=None):
        source = _bound_source(organization=organization, lead=lead, supplied=source_message)
        effective_actions = actions
        if _inside_ai_turn(organization=organization):
            effective_actions = _filter_stage_actions(
                organization=organization, lead=lead, actions=actions, source_message=source,
            )
        extra = {"source_message": source} if source is not None else {}
        return current_execute(
            self, organization=organization, lead=lead,
            actions=effective_actions, actor=actor, **extra,
        )

    CRMActionExecutor.execute = execute
    _INSTALLED = True
