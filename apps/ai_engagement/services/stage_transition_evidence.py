from __future__ import annotations

from copy import deepcopy


_INSTALLED = False


def _latest_inbound_text(lead, source_message=None) -> str:
    try:
        from apps.channels.models import WhatsAppMessage

        if source_message is not None:
            from apps.ai_engagement.services.tenant_guard import TenantGuard

            TenantGuard(lead.organization).validate_message(source_message, lead=lead)
            if source_message.direction != WhatsAppMessage.Direction.INBOUND:
                return ""
            return str(source_message.body or "").strip()
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


def _configured_completion_allowed(*, organization, lead, destination) -> bool:
    """Allow the exact configured completion target from verified backend state.

    A qualification-completion rule is deterministic configuration, not a model
    guess. Once the Qualification Engine says the configured requirements are
    complete, the final customer answer does not need to mention the target stage
    name. The target itself is still resolved and tenant-validated by the backend.
    """
    try:
        from apps.ai_engagement.services import qualification_state as qs
        from apps.ai_engagement.services.qualification_execution_contract import _config
        from apps.ai_engagement.services.transactional_turn_runtime import (
            _requirements_for_turn,
        )

        requirements = _requirements_for_turn(
            organization=organization,
            lead=lead,
        )
        if not requirements:
            return False
        state = qs.state_for_lead(lead, requirements=requirements)
        if str(state.get("qualification_status") or "").casefold() != "completed":
            return False

        config = _config(
            organization=organization,
            requirements=requirements,
        )
        target = config.get("completion_stage")
        return bool(
            isinstance(target, dict)
            and str(target.get("id") or "") == str(destination.id)
        )
    except Exception:
        return False


def _nonqualified_evidence_matches(*, organization, destination, latest_text: str) -> bool:
    if not latest_text:
        return False

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

    # For ordinary model-proposed routing without a deterministic completion
    # rule, require explicit destination evidence when no description/rule matches.
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

        destination = _destination_for_action(
            organization=organization,
            action=action,
        )
        if destination is None:
            # Let the executor raise its normal tenant/id validation error.
            filtered.append(deepcopy(action))
            continue

        # Deterministic qualification completion has its own backend evidence:
        # completed qualification state + exact configured completion target.
        if _configured_completion_allowed(
            organization=organization,
            lead=lead,
            destination=destination,
        ):
            filtered.append(deepcopy(action))
            continue

        # Ordinary model-proposed stage movement still requires customer/config
        # evidence. Missing inbound evidence must fail closed for a real AI turn.
        if _nonqualified_evidence_matches(
            organization=organization,
            destination=destination,
            latest_text=latest_text,
        ):
            filtered.append(deepcopy(action))
        else:
            try:
                from apps.ai_engagement.services.trace_service import append
                append("crm_actions", "rejections", {"type": "pipeline_transition",
                       "reason_code": "STAGE_EVIDENCE_REQUIRED", "stage_id": str(destination.pk)})
            except Exception:
                pass  # Observability never grants or revokes mutation permission.
    return filtered


def _inside_ai_turn(*, organization) -> bool:
    """Return True only inside the production API/Hosted AI execution boundary.

    AI Trace installs last and keeps a ContextVar buffer active for the whole
    customer turn even when persistence of the diagnostic row fails.  That makes
    it a transport-neutral execution-scope marker without coupling authorization
    to the database trace row itself.
    """
    try:
        from apps.ai_engagement.services.trace_service import current

        trace = current()
    except Exception:
        return False
    return bool(
        trace is not None
        and str(getattr(trace, "organization_id", "") or "") == str(organization.id)
    )


def install_stage_transition_evidence() -> None:
    """Enforce evidence for AI routing without changing the CRM service contract."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.crm_executor import CRMActionExecutor

    current_execute = CRMActionExecutor.execute

    def execute(self, *, organization, lead, actions, actor=None, source_message=None):
        # The executor is also a deterministic backend service and is used by
        # non-AI code/tests. Tenant ownership and schema validation belong there;
        # model-evidence filtering belongs only to an actual API/Hosted AI turn.
        effective_actions = actions
        if _inside_ai_turn(organization=organization):
            effective_actions = _filter_stage_actions(
                organization=organization,
                lead=lead,
                actions=actions,
                source_message=source_message,
            )
        return current_execute(
            self,
            organization=organization,
            lead=lead,
            actions=effective_actions,
            actor=actor,
            source_message=source_message,
        )

    CRMActionExecutor.execute = execute
    _INSTALLED = True
