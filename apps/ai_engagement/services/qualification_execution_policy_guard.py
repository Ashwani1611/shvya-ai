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


def _contains_identifier(text: str, value: str) -> bool:
    text = str(text or "")
    value = str(value or "").strip()
    if not text or len(value) < 3:
        return False
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        )
    )


def _strip_protected_generated_text(message: str, state: dict) -> str:
    """Remove actual internal labels/IDs without censoring customer vocabulary."""
    identifiers = [
        str(item).strip()
        for item in state.get("protected_configuration_labels") or []
        if str(item or "").strip()
    ]
    if not identifiers:
        return str(message or "").strip()

    text = str(message or "").strip()
    response_plan = state.get("response_plan") if isinstance(state, dict) else None

    # A response-plan reply contains generated language first and exact
    # backend-authored customer content after the blank line. Never filter the
    # authored question/options/final acknowledgement value.
    blocks = text.split("\n\n")
    generated = blocks[0] if blocks else ""
    suffix = blocks[1:] if isinstance(response_plan, dict) and len(blocks) > 1 else []
    if not isinstance(response_plan, dict):
        generated = text

    parts = re.split(r"(?<=[.!?])\s+|\n+", generated)
    kept = [
        part.strip()
        for part in parts
        if part.strip()
        and not any(_contains_identifier(part, item) for item in identifiers)
    ]
    cleaned = " ".join(kept).strip()
    if suffix:
        return "\n\n".join([cleaned, *suffix]).strip()
    return cleaned


def install_qualification_execution_policy_guard() -> None:
    """Enforce the organization-configured qualification execution contract.

    Natural-language interpretation may come from the model, but qualification
    attributes, completion, configured stage actions, execution results and final
    response mode are backend-owned.
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
    from apps.ai_engagement.services.engagement import EngagementError, EngagementService
    from apps.ai_engagement.services.qualification_execution_contract import (
        _config,
        _norm,
        _plan_from_reconciled,
        _requirement_ref,
        _stage_success,
    )

    # ------------------------------------------------------------------
    # Reconciled output protection: IDs/keys/config section labels only.
    # Display names such as "Budget" or configured stage names are ordinary
    # customer/business vocabulary and must not be mistaken for secrets.
    # ------------------------------------------------------------------
    current_build_snapshot = StateReconciler.build

    @wraps(current_build_snapshot)
    def build_snapshot(
        self,
        *,
        lead,
        source_message_id,
        execution_results=None,
        structured_decision=None,
    ):
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
        protected: set[str] = {
            "Acknowledgment message",
            "Completion Message",
            "Qualification Requirements",
            "Attribute Mapped",
            "Stage Shifting",
        }
        for aliases in _SECTION_ALIASES.values():
            protected.update(
                str(item).strip() for item in aliases if str(item).strip()
            )
        for requirement in requirements:
            for key in ("id", "stable_id"):
                value = str(requirement.get(key) or "").strip()
                if value:
                    protected.add(value)
            protected.update(
                str(item).strip()
                for item in requirement.get("legacy_ids") or []
                if str(item or "").strip()
            )
        protected.update(
            str(key).strip()
            for key in AttributeDefinition.objects.filter(
                organization=lead.organization
            ).values_list("key", flat=True)
            if str(key or "").strip()
        )
        protected.update(
            str(stage_id)
            for stage_id in Stage.objects.filter(
                pipeline__organization=lead.organization,
                pipeline__is_active=True,
                is_active=True,
            ).values_list("id", flat=True)
        )
        snapshot["protected_configuration_labels"] = sorted(
            protected,
            key=lambda item: (-len(item), item.casefold()),
        )
        return snapshot

    StateReconciler.build = build_snapshot

    # Keep caller objects synchronized with rows changed by deterministic
    # pre-generation execution.
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

    # ------------------------------------------------------------------
    # Completion stage: exact organization Stage Shifting config only.
    # ------------------------------------------------------------------
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
        target = config.get("completion_stage")
        if not isinstance(target, dict) or target.get("id") is None:
            return None
        return {
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": str(target["id"])},
        }

    runtime._qualified_action = configured_completion_action

    # ------------------------------------------------------------------
    # Model-interpreted qualification answers: discard model-selected attribute
    # writes and rebuild exact requirement -> configured attribute actions.
    # ------------------------------------------------------------------
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
            actions = [
                deepcopy(action)
                for action in getattr(decision, "crm_actions", []) or []
                if isinstance(action, dict)
                and action.get("type") != "attribute_updates"
            ]
            exact_updates = []
            for update in qualification_updates:
                requirement = _requirement_ref(
                    str(update.get("requirement_id") or ""),
                    requirements,
                )
                if requirement is None:
                    continue
                requirement_id = str(requirement.get("id") or "")
                attribute_key = config.get("mappings", {}).get(requirement_id)
                if attribute_key:
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
        revised = {**snapshot, "execution_results": execution_results}
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

    # ------------------------------------------------------------------
    # Ensure every persistent EngagementService path enters the same contract
    # before the older service-internal direct-answer capture can run.
    # ------------------------------------------------------------------
    current_engage = EngagementService.engage

    @wraps(current_engage)
    def engage(
        self,
        *,
        organization,
        lead,
        knowledge_query=None,
        context=None,
    ):
        if (
            getattr(organization, "_meta", None) is not None
            and getattr(lead, "_meta", None) is not None
            and getattr(lead, "pk", None) is not None
        ):
            try:
                from apps.ai_engagement.services.ai_permissions import AIPermissionService

                source = (
                    lead.whatsapp_messages.filter(
                        organization=organization,
                        direction="inbound",
                    )
                    .order_by("-created_at", "-id")
                    .first()
                )
                permission = (
                    AIPermissionService().evaluate(
                        organization=organization,
                        lead=lead,
                        latest_inbound=source,
                    )
                    if source is not None
                    else None
                )
                if permission and permission.allowed and source is not None:
                    contract_module.resolve_before_generation(
                        organization=organization,
                        lead=lead,
                        source_message_id=source.pk,
                        account_id=source.account_id,
                    )
            except Exception:
                # The normal engagement path remains fail-soft; task-level
                # execution records/logging will expose persistent failures.
                pass
        return current_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )

    EngagementService.engage = engage

    # ------------------------------------------------------------------
    # Final customer output: verified stage claims + internal ID protection.
    # ------------------------------------------------------------------
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
