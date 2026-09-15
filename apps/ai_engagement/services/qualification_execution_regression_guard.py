from __future__ import annotations

from copy import deepcopy
from functools import wraps


_INSTALLED = False
_MAPPING_ERROR_CODES = {
    "invalid_attribute_mapping_rule",
    "unknown_requirement_mapping_reference",
    "unknown_attribute_mapping_reference",
    "ambiguous_attribute_mapping",
}


def _has_mapping_policy(*, organization) -> bool:
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement_instruction_policy import section_lines

    info = OrgInfo.objects.filter(organization=organization).first()
    raw = str(getattr(info, "engagement_instructions", "") or "")
    return bool(section_lines(raw, "attribute_mapped"))


def _has_stage_policy(*, organization) -> bool:
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement_instruction_policy import section_lines

    info = OrgInfo.objects.filter(organization=organization).first()
    raw = str(getattr(info, "engagement_instructions", "") or "")
    return bool(section_lines(raw, "stage_shifting"))


def _mapping_errors(config) -> list[dict]:
    return [
        deepcopy(item)
        for item in config.get("errors") or []
        if isinstance(item, dict) and item.get("code") in _MAPPING_ERROR_CODES
    ]


def _persist_reconciled_stage(*, lead, source_message_id, result, requirements, config):
    """Make a configured completion stage authoritative before final generation.

    This is deliberately a readback/reconciliation step, not a model-side stage
    decision. It only runs when the backend qualification state is complete and
    the organization has one resolved completion target.
    """
    from apps.ai_engagement.services import qualification_execution_contract as contract
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services.canonical_architecture import StateReconciler
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor

    state = qs.state_for_lead(lead, requirements=requirements)
    if str(state.get("qualification_status") or "").casefold() != "completed":
        return result

    target = config.get("completion_stage")
    if not isinstance(target, dict) or target.get("id") is None or _mapping_errors(config):
        return result

    target_id = str(target["id"])
    execution_results = [
        deepcopy(item)
        for item in (result.get("execution_results") or result.get("results") or [])
        if isinstance(item, dict)
    ]
    failed = any(
        item.get("type") == "pipeline_transition"
        and str(item.get("status") or "").casefold() == "failed"
        for item in execution_results
    )

    lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
    if str(lead.stage_id or "") != target_id and not failed:
        try:
            CRMActionExecutor().execute(
                organization=lead.organization,
                lead=lead,
                actions=[
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {"stage_id": target_id},
                    }
                ],
            )
        except Exception as exc:
            execution_results.append(
                {
                    "type": "pipeline_transition",
                    "status": "failed",
                    "code": "stage_transition_failed",
                    "detail": str(exc)[:500],
                }
            )
        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])

    verified = contract._verify_stage(lead, target)
    execution_results = [
        item
        for item in execution_results
        if item.get("type") != "pipeline_transition"
    ] + [verified]

    snapshot = result.get("reconciled_state") if isinstance(result, dict) else None
    structured = (
        deepcopy(snapshot.get("structured_decision"))
        if isinstance(snapshot, dict) and isinstance(snapshot.get("structured_decision"), dict)
        else {}
    )
    reconciler = StateReconciler()
    revised = reconciler.build(
        lead=lead,
        source_message_id=source_message_id,
        execution_results=execution_results,
        structured_decision=structured,
    )
    plan = contract._plan_from_reconciled(
        lead=lead,
        source_message_id=source_message_id,
        snapshot=revised,
    )
    if plan:
        revised["response_plan"] = plan
    reconciler.persist_for_source(
        lead=lead,
        source_message_id=source_message_id,
        snapshot=revised,
    )

    return {
        **result,
        "results": execution_results,
        "execution_results": execution_results,
        "stage_id": str(lead.stage_id or ""),
        "reconciled_state": revised,
        **({"response_plan": plan} if plan else {}),
    }


def install_qualification_execution_regression_guard() -> None:
    """Keep the strict contract compatible with established organizations.

    The new contract is authoritative only after a requirement was actually
    asked, and only replaces legacy attribute/stage behavior when the organization
    explicitly configured the corresponding AI Brain policy section.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_execution_contract as contract
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.canonical_architecture import ResponseActionValidator

    # Never consume the first greeting as an answer to Q1. A direct answer may be
    # applied only to the requirement the backend actually recorded as asked.
    current_pre_resolve = contract.resolve_before_generation

    @wraps(current_pre_resolve)
    def resolve_before_generation(*, organization, lead, source_message_id, account_id=None):
        requirements = runtime._requirements_for_turn(
            organization=organization,
            lead=lead,
        )
        state = qs.state_for_lead(lead, requirements=requirements)
        last_asked = str(state.get("last_asked_requirement_id") or "").strip()
        active = str(state.get("current_requirement_id") or "").strip()
        if not last_asked or not active or last_asked != active:
            return {"applied": False, "reason": "no_answered_requirement"}

        result = current_pre_resolve(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            account_id=account_id,
        )
        if not isinstance(result, dict) or not result.get("applied"):
            return result

        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        config = contract._config(
            organization=organization,
            requirements=requirements,
        )
        if _has_stage_policy(organization=organization):
            result = _persist_reconciled_stage(
                lead=lead,
                source_message_id=source_message_id,
                result=result,
                requirements=requirements,
                config=config,
            )
        return result

    contract.resolve_before_generation = resolve_before_generation

    # Explicit configured completion stage wins. Organizations without a Stage
    # shifting policy retain the existing deterministic Qualified transition.
    configured_completion_action = runtime._qualified_action

    def completion_action(*, lead, qualification_state):
        action = configured_completion_action(
            lead=lead,
            qualification_state=qualification_state,
        )
        if action is not None:
            return action
        if str(qualification_state.get("qualification_status") or "").casefold() != "completed":
            return None
        if _has_stage_policy(organization=lead.organization):
            # Explicit but invalid/unresolved configuration fails closed instead
            # of silently substituting the legacy Qualified stage.
            return None
        if qs.normalize_stage_name(getattr(getattr(lead, "stage", None), "name", "")) != "new lead":
            return None
        stage_id = str(qualification_state.get("qualified_stage_id") or "").strip()
        if not stage_id:
            return None
        return {
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": stage_id},
        }

    runtime._qualified_action = completion_action

    # Strict exact requirement->attribute projection applies when Attribute
    # mapped exists. Existing organizations without that section keep already
    # validated CRM actions instead of losing their attributes during rollout.
    policy_resolve = runtime._resolve_state_before_response
    pre_policy_resolve = getattr(policy_resolve, "__wrapped__", None)

    @wraps(policy_resolve)
    def resolve(*, organization, lead, source_message_id, decision, account_id=None):
        has_qualification_updates = bool(getattr(decision, "qualification_updates", []) or [])
        use_strict_mapping = _has_mapping_policy(organization=organization)
        resolver = policy_resolve
        if has_qualification_updates and not use_strict_mapping and callable(pre_policy_resolve):
            resolver = pre_policy_resolve

        result = resolver(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            decision=decision,
            account_id=account_id,
        )
        if not isinstance(result, dict) or not result.get("applied"):
            return result

        if _has_stage_policy(organization=organization):
            lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
            requirements = runtime._requirements_for_turn(
                organization=organization,
                lead=lead,
            )
            config = contract._config(
                organization=organization,
                requirements=requirements,
            )
            result = _persist_reconciled_stage(
                lead=lead,
                source_message_id=source_message_id,
                result=result,
                requirements=requirements,
                config=config,
            )
        return result

    runtime._resolve_state_before_response = resolve

    # Internal-label filtering is only meaningful on backend response-plan turns.
    # Without a plan, the normal backend-selected next question is valid customer
    # content and must not be mistaken for leaked configuration.
    policy_validate = ResponseActionValidator.validate
    pre_policy_validate = getattr(policy_validate, "__wrapped__", None)

    @wraps(policy_validate)
    def validate(self, *, decision, reconciled_state):
        state = reconciled_state if isinstance(reconciled_state, dict) else {}
        if not isinstance(state.get("response_plan"), dict) and callable(pre_policy_validate):
            return pre_policy_validate(
                self,
                decision=decision,
                reconciled_state=reconciled_state,
            )
        return policy_validate(
            self,
            decision=decision,
            reconciled_state=reconciled_state,
        )

    ResponseActionValidator.validate = validate
    _INSTALLED = True
