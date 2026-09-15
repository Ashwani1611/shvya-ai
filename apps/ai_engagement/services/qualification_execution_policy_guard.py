from __future__ import annotations

import re
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


def _contains_label(text: str, label: str) -> bool:
    text = str(text or "")
    label = str(label or "").strip()
    if not text or len(label) < 3:
        return False
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        )
    )


def _strip_protected_generated_text(message: str, state: dict) -> str:
    labels = [
        str(item).strip()
        for item in state.get("protected_configuration_labels") or []
        if str(item or "").strip()
    ]
    if not labels:
        return str(message or "").strip()

    # Response-plan controlled qualification replies are assembled as:
    # generated acknowledgement + blank line + backend-authored content. Protect
    # only the generated acknowledgement; configured question/options/final value
    # must pass through exactly as authored.
    blocks = str(message or "").strip().split("\n\n")
    generated = blocks[0] if blocks else ""
    suffix = blocks[1:] if len(blocks) > 1 else []

    parts = re.split(r"(?<=[.!?])\s+|\n+", generated)
    kept = [
        part.strip()
        for part in parts
        if part.strip()
        and not any(_contains_label(part, label) for label in labels)
    ]
    cleaned = " ".join(kept).strip()
    if suffix:
        return "\n\n".join([cleaned, *suffix]).strip()
    return cleaned


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
    from apps.ai_engagement.services import qualification_execution_contract as contract_module
    from apps.ai_engagement.services.canonical_architecture import (
        ResponseActionValidator,
        StateReconciler,
        _QUALIFIED_CLAIM_RE,
    )
    from apps.ai_engagement.services.engagement import EngagementError
    from apps.ai_engagement.services.qualification_execution_contract import (
        _config,
        _norm,
        _plan_from_reconciled,
        _requirement_ref,
        _stage_success,
    )

    # Reconciled state carries dynamic customer-output protection metadata. This
    # is derived from the organization configuration/CRM schema, not from one
    # organization's current labels.
    current_build_snapshot = StateReconciler.build

    @wraps(current_build_snapshot)
    def build_snapshot(self, *, lead, source_message_id, execution_results=None, structured_decision=None):
        snapshot = current_build_snapshot(
            self,
            lead=lead,
            source_message_id=source_message_id,
            execution_results=execution_results,
            structured_decision=structured_decision,
        )
        from apps.ai_engagement.services.engagement_instruction_policy import _SECTION_ALIASES
        from apps.crm.models import AttributeDefinition, Stage

        requirements = runtime._requirements_for_turn(
            organization=lead.organization,
            lead=lead,
        )
        labels: set[str] = set()
        for aliases in _SECTION_ALIASES.values():
            labels.update(str(item).strip() for item in aliases if str(item).strip())
        for requirement in requirements:
            for key in ("id", "stable_id"):
                value = str(requirement.get(key) or "").strip()
                if value:
                    labels.add(value)
            labels.update(
                str(item).strip()
                for item in requirement.get("legacy_ids") or []
                if str(item or "").strip()
            )
        for definition in AttributeDefinition.objects.filter(
            organization=lead.organization
        ).values("key", "name"):
            labels.update(
                str(definition.get(key) or "").strip()
                for key in ("key", "name")
                if str(definition.get(key) or "").strip()
            )
        labels.update(
            str(name).strip()
            for name in Stage.objects.filter(
                pipeline__organization=lead.organization,
                pipeline__is_active=True,
                is_active=True,
            ).values_list("name", flat=True)
            if str(name or "").strip()
        )
        snapshot["protected_configuration_labels"] = sorted(
            labels,
            key=lambda item: (-len(item), item.casefold()),
        )
        return snapshot

    StateReconciler.build = build_snapshot

    # Keep callers synchronized with the row that was locked and mutated by the
    # pre-generation resolver. A stale in-memory Lead must never overwrite a
    # just-persisted qualification answer on the next requirement transition.
    current_pre_resolve = contract_module.resolve_before_generation

    @wraps(current_pre_resolve)
    def pre_resolve(*, organization, lead, source_message_id, account_id=None):
        result = current_pre_resolve(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            account_id=account_id,
        )
        if isinstance(result, dict) and result.get("applied"):
            lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        return result

    contract_module.resolve_before_generation = pre_resolve

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

    # Qualification completion is never accepted as evidence that a configured
    # CRM stage transition succeeded. A stage-movement claim needs the reconciled
    # target stage plus a verified execution result.
    current_validate = ResponseActionValidator.validate

    @wraps(current_validate)
    def validate(self, *, decision, reconciled_state):
        validated = current_validate(
            self,
            decision=decision,
            reconciled_state=reconciled_state,
        )
        state = reconciled_state if isinstance(reconciled_state, dict) else {}
        message = str(getattr(validated, "message", "") or "")
        if _QUALIFIED_CLAIM_RE.search(message) and not _stage_success(state):
            if not isinstance(state.get("response_plan"), dict):
                raise EngagementError(
                    "Customer response claimed an unverified stage transition."
                )
        protected = _strip_protected_generated_text(message, state)
        if not protected and message:
            raise EngagementError(
                "Customer response exposed internal AI Brain/CRM configuration."
            )
        return replace(validated, message=protected)

    ResponseActionValidator.validate = validate
    _INSTALLED = True
