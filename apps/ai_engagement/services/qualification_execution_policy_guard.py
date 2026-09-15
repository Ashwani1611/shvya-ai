from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import wraps


_INSTALLED = False
_MAPPING_ERROR_CODES = {
    "invalid_attribute_mapping_rule",
    "unknown_requirement_mapping_reference",
    "unknown_attribute_mapping_reference",
    "ambiguous_attribute_mapping",
}


def _mapping_errors(config):
    return [
        deepcopy(item)
        for item in config.get("errors") or []
        if isinstance(item, dict) and item.get("code") in _MAPPING_ERROR_CODES
    ]


def install_qualification_execution_policy_guard() -> None:
    """Make model-interpreted qualification turns obey the same CRM contract.

    High-confidence option/yes-no answers are pre-resolved by
    qualification_execution_contract. Natural-language answers can still reach
    the legacy transactional resolver through an LLM qualification update. This
    guard removes fuzzy qualification attribute actions from that route, rebuilds
    them only from explicit configured mappings, and makes completion stage
    execution use the configured Stage Shifting rule rather than a canonical
    stage name.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.canonical_architecture import StateReconciler
    from apps.ai_engagement.services.qualification_execution_contract import (
        _config,
        _norm,
        _plan_from_reconciled,
        _requirement_ref,
    )

    def configured_completion_action(*, lead, qualification_state):
        if _norm(qualification_state.get("qualification_status")) != "completed":
            return None
        requirements = runtime._requirements_for_turn(
            organization=lead.organization,
            lead=lead,
        )
        config = _config(
            organization=lead.organization,
            requirements=requirements,
        )
        if _mapping_errors(config):
            return None
        target = config.get("completion_stage")
        if not isinstance(target, dict) or target.get("id") is None:
            return None
        return {
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": str(target["id"])},
        }

    # The completion action is now configuration-owned. Qualification completion
    # and a successful stage transition remain separate pieces of backend state.
    runtime._qualified_action = configured_completion_action

    current_resolve = runtime._resolve_state_before_response
    reconciler = StateReconciler()

    @wraps(current_resolve)
    def resolve(
        *,
        organization,
        lead,
        source_message_id,
        decision,
        account_id=None,
    ):
        qualification_updates = [
            deepcopy(item)
            for item in getattr(decision, "qualification_updates", []) or []
            if isinstance(item, dict)
        ]
        config = None

        if qualification_updates:
            requirements = runtime._requirements_for_turn(
                organization=organization,
                lead=lead,
            )
            config = _config(
                organization=organization,
                requirements=requirements,
            )

            # Never let a model/fuzzy runtime choose a qualification attribute.
            # Keep unrelated workflow/contact/reminder actions, then project only
            # exact configured requirement -> attribute bindings.
            actions = [
                deepcopy(action)
                for action in getattr(decision, "crm_actions", []) or []
                if isinstance(action, dict)
                and action.get("type") != "attribute_updates"
            ]
            exact_updates = []
            if not _mapping_errors(config):
                for update in qualification_updates:
                    requirement = _requirement_ref(
                        str(update.get("requirement_id") or ""),
                        requirements,
                    )
                    if requirement is None:
                        continue
                    requirement_id = str(requirement.get("id") or "")
                    attribute_key = config.get("mappings", {}).get(requirement_id)
                    if not attribute_key:
                        continue
                    exact_updates.append(
                        {
                            "key": attribute_key,
                            "value": update.get("value"),
                        }
                    )
            if exact_updates:
                by_key = {
                    str(item["key"]): item
                    for item in exact_updates
                    if item.get("key")
                }
                actions.insert(
                    0,
                    {
                        "type": "attribute_updates",
                        "updates": list(by_key.values()),
                    },
                )
            decision = replace(decision, crm_actions=actions)

        result = current_resolve(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            decision=decision,
            account_id=account_id,
        )

        if (
            not isinstance(result, dict)
            or not result.get("applied")
            or config is None
        ):
            return result

        errors = _mapping_errors(config)
        snapshot = result.get("reconciled_state")
        if not errors or not isinstance(snapshot, dict):
            return result

        # Surface invalid configured mappings in the authoritative execution
        # record. Do not silently substitute another attribute.
        execution_results = [
            deepcopy(item)
            for item in snapshot.get("execution_results") or []
            if isinstance(item, dict)
        ]
        existing = {
            (str(item.get("code") or ""), str(item.get("detail") or ""))
            for item in execution_results
        }
        for error in errors:
            marker = (
                str(error.get("code") or ""),
                str(error.get("detail") or ""),
            )
            if marker not in existing:
                execution_results.append(error)

        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        revised = {
            **snapshot,
            "execution_results": execution_results,
        }
        plan = _plan_from_reconciled(
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
        return {**result, "reconciled_state": revised}

    runtime._resolve_state_before_response = resolve
    _INSTALLED = True
