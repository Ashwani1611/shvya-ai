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

    # Protect only an exact backend-authored trailing block, never arbitrary
    # model text after a blank line. Ordinary ANSWER_THEN_QUALIFY turns may have
    # no qualification-write plan, but their reconciled next question is still
    # authored by the organization and must not be censored as an internal ID.
    trusted = []
    if isinstance(response_plan, dict):
        trusted.extend([
            str((response_plan.get("next_requirement") or {}).get("rendered") or "").strip(),
            str((response_plan.get("final_configured_acknowledgement") or {}).get("value") or "").strip(),
        ])
    trusted.append(str((state.get("customer_next_requirement") or {}).get("question") or "").strip())
    generated = text
    suffix = []
    for authored in sorted(set(trusted), key=len, reverse=True):
        if authored and (text == authored or text.endswith("\n\n" + authored)):
            generated = text[:-len(authored)].rstrip()
            suffix = [authored]
            break

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


def _strip_unverified_stage_claim(message: str, state: dict) -> str:
    """Remove generated claims about a configured stage move that did not execute."""
    if _stage_success_for_state(state):
        return str(message or "").strip()

    plan = state.get("response_plan") if isinstance(state, dict) else None
    execution = plan.get("execution_results") if isinstance(plan, dict) else None
    stage_info = execution.get("stage_transition") if isinstance(execution, dict) else None
    target_id = str(stage_info.get("target_stage_id") or "") if isinstance(stage_info, dict) else ""
    if not target_id:
        return str(message or "").strip()

    try:
        from apps.crm.models import Stage

        target_name = Stage.objects.filter(pk=target_id).values_list("name", flat=True).first()
    except Exception:
        target_name = None
    target_name = str(target_name or "").strip()
    if not target_name:
        return str(message or "").strip()

    text = str(message or "").strip()
    blocks = text.split("\n\n")
    generated = blocks[0] if blocks else ""
    suffix = blocks[1:]
    parts = re.split(r"(?<=[.!?])\s+|\n+", generated)
    kept = [
        part.strip()
        for part in parts
        if part.strip() and not _contains_identifier(part, target_name)
    ]
    cleaned = " ".join(kept).strip()
    if suffix:
        return "\n\n".join([cleaned, *suffix]).strip()
    return cleaned


def _stage_success_for_state(state: dict) -> bool:
    """Late-bound wrapper so the output helper can use the installed contract check."""
    try:
        from apps.ai_engagement.services.qualification_execution_contract import _stage_success

        return bool(_stage_success(state))
    except Exception:
        return False


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
        _completion_target,
        _config,
        _configured_completion_reminders,
        _mapping_keys,
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
        current_id = str((snapshot.get("qualification") or {}).get("current_requirement_id") or "")
        next_requirement = next((item for item in requirements if str(item.get("id")) == current_id), None)
        if next_requirement and (snapshot.get("qualification") or {}).get("status") != "completed":
            snapshot["customer_next_requirement"] = deepcopy(next_requirement)
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
    # Completion stage: explicit configured target first, then the validated
    # same-pipeline Qualified stage already exposed by qualification state.
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
        target = _completion_target(
            lead=lead,
            state=qualification_state,
            config=config,
        )
        if not isinstance(target, dict) or target.get("id") is None:
            return None
        return {
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": str(target["id"])},
        }

    runtime._qualified_action = configured_completion_action

    # ------------------------------------------------------------------
    # Model-interpreted qualification answers: discard model-selected attribute
    # and stage writes, then rebuild only exact configured backend actions.
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
            # Qualification-owned mapped keys are rebuilt from deterministic
            # backend configuration. Preserve unrelated evidence-validated CRM
            # facts from the same customer message (for example company/team
            # size volunteered alongside a qualification answer), while never
            # allowing the model to override a configured qualification mapping.
            proposed_attribute_updates = []
            actions = []
            for action in getattr(decision, "crm_actions", []) or []:
                if not isinstance(action, dict) or not action.get("type"):
                    continue
                if action.get("type") == "attribute_updates":
                    proposed_attribute_updates.extend(
                        deepcopy(item)
                        for item in action.get("updates") or []
                        if isinstance(item, dict) and item.get("key")
                    )
                    continue
                if action.get("type") in {"pipeline_transition", "create_reminder"}:
                    continue
                actions.append(deepcopy(action))

            exact_updates = []
            deterministic_keys = set()
            for update in qualification_updates:
                requirement = _requirement_ref(
                    str(update.get("requirement_id") or ""),
                    requirements,
                )
                if requirement is None:
                    continue
                requirement_id = str(requirement.get("id") or "")
                for attribute_key in _mapping_keys(config, requirement_id):
                    deterministic_keys.add(str(attribute_key))
                    exact_updates.append(
                        {
                            "key": attribute_key,
                            "value": update.get("value"),
                        }
                    )

            # Keep only distinct non-qualification facts from the already
            # policy-filtered decision. If a model proposes the same value as a
            # qualification answer under another key, treat it as an attempted
            # fuzzy/shadow mapping and discard it. This preserves the existing
            # exact-mapping contract while allowing genuinely separate facts
            # volunteered in the same message.
            qualification_values = {
                re.sub(r"\\s+", " ", str(item.get("value") or "")).strip().casefold()
                for item in qualification_updates
                if str(item.get("value") or "").strip()
            }
            by_key = {}
            for item in proposed_attribute_updates:
                key = str(item.get("key") or "")
                value = re.sub(
                    r"\\s+", " ", str(item.get("value") or "")
                ).strip().casefold()
                if not key or key in deterministic_keys or value in qualification_values:
                    continue
                by_key[key] = item
            for item in exact_updates:
                if item.get("key"):
                    by_key[str(item["key"])] = item
            if by_key:
                actions.insert(
                    0,
                    {
                        "type": "attribute_updates",
                        "updates": list(by_key.values()),
                    },
                )

            # Apply authored completion-reminder rules only if this inbound answer
            # actually completes qualification. A reminder is never invented from
            # qualification completion alone.
            from apps.ai_engagement.services import qualification_state as qs

            source = (
                lead.whatsapp_messages.filter(
                    pk=source_message_id,
                    organization=organization,
                    direction="inbound",
                )
                .values("id", "body", "direction")
                .first()
            )
            if source is not None:
                # Preserve the existing deterministic explicit date/time or call
                # reminder behavior. This rebuilds the action from customer
                # evidence instead of trusting a model-proposed due_at.
                from apps.ai_engagement.services.crm_routing_reliability import (
                    _ensure_datetime_reminder,
                )

                _ensure_datetime_reminder(
                    actions,
                    str(source.get("body") or ""),
                )

                base_state = qs.state_for_lead(lead, requirements=requirements)
                try:
                    projected = qs.project_answer_updates(
                        state=base_state,
                        requirements=requirements,
                        updates=qualification_updates,
                        messages=[
                            {
                                "id": str(source["id"]),
                                "body": source.get("body") or "",
                                "direction": "inbound",
                            }
                        ],
                    )
                except ValueError:
                    projected = base_state
                if _norm(projected.get("qualification_status")) == "completed":
                    actions.extend(_configured_completion_reminders(config))

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
        message = _strip_unverified_stage_claim(message, state)
        protected = _strip_protected_generated_text(message, state)
        if not protected and message:
            raise EngagementError(
                "Customer response exposed internal AI Brain/CRM configuration."
            )
        return replace(validated, message=protected)

    ResponseActionValidator.validate = validate
    _INSTALLED = True
