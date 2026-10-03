"""Authoritative qualification execution orchestration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from django.db import transaction
from django.utils import timezone

from .common import _CONTRACT_KEY, _PLAN_KEY, _norm, _processing, _save_processing
from .completion import _completion_target, _configured_completion_reminders
from .config import _config, _mapped_value, _mapping_keys
from .evidence import (
    _additional_explicit_updates,
    _asked_requirement,
    _persist_plan_only,
    _verify_attribute,
    _verify_stage,
)
from .planning import _clarification_plan, _plan, _start_plan


def resolve_before_generation(
    *,
    organization,
    lead,
    source_message_id,
    account_id=None,
) -> dict[str, Any]:
    """Resolve deterministic qualification work before customer response generation."""
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services.canonical_architecture import StateReconciler
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor
    from apps.ai_engagement.services.transactional_turn_runtime import (
        _mark_state_resolved,
        _persist_updates_against_requirements,
        _requirements_for_turn,
    )
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead

    with transaction.atomic():
        locked = (
            Lead.objects.select_for_update()
            .select_related("organization", "pipeline", "stage")
            .get(pk=lead.pk, organization=organization)
        )
        query = WhatsAppMessage.objects.select_for_update().filter(
            pk=source_message_id,
            organization=organization,
            lead=locked,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        if account_id is not None:
            query = query.filter(account_id=account_id)
        source = query.first()
        if source is None:
            return {"applied": False, "reason": "source_message_not_found"}

        processing = _processing(source)
        if (
            isinstance(processing.get(_CONTRACT_KEY), dict)
            and processing[_CONTRACT_KEY].get("applied")
        ):
            return {"applied": False, "reason": "already_applied"}

        requirements = _requirements_for_turn(
            organization=organization,
            lead=locked,
        )
        state = qs.state_for_lead(locked, requirements=requirements)
        if not requirements:
            return {"applied": False, "reason": "no_qualification_requirements"}
        if _norm(state.get("qualification_status")) == "completed":
            return {"applied": False, "reason": "qualification_already_complete"}

        requirement = _asked_requirement(
            state=state,
            requirements=requirements,
        )
        if requirement is None:
            next_item = qs.next_requirement(
                requirements,
                state.get("requirement_states") or {},
            )
            if not isinstance(next_item, dict):
                return {"applied": False, "reason": "no_active_requirement"}
            plan = _start_plan(next_item)
            _persist_plan_only(
                lead=locked,
                source=source,
                plan=plan,
                intent="qualification_start",
            )
            return {
                "applied": False,
                "reason": "qualification_start",
                "response_plan": plan,
            }

        active_id = str(requirement.get("id") or "")
        classified = qs._classify_direct_reply(
            text=source.body,
            question=str(requirement.get("question") or requirement.get("label") or ""),
        )
        if classified is None:
            return {"applied": False, "reason": "requires_llm_interpretation"}

        answer_status, answer, confidence = classified
        if answer_status != qs.REQUIREMENT_ANSWERED:
            plan = _clarification_plan(
                source=source,
                requirement=requirement,
            )
            _persist_plan_only(
                lead=locked,
                source=source,
                plan=plan,
                intent="qualification_clarification",
            )
            return {
                "applied": False,
                "reason": "qualification_clarification",
                "response_plan": plan,
            }

        update = {
            "requirement_id": active_id,
            "value": answer,
            "source_message_id": str(source.id),
            "evidence": str(source.body or "").strip(),
        }
        updates = [update, *_additional_explicit_updates(
            organization=organization, requirements=requirements, state=state,
            source=source, active_id=active_id)]
        projected = qs.project_answer_updates(
            state=state,
            requirements=requirements,
            updates=updates,
            messages=[
                {
                    "id": str(source.id),
                    "body": source.body,
                    "direction": "inbound",
                }
            ],
        )
        config = _config(
            organization=organization,
            requirements=requirements,
        )

        # Build the complete authoritative execution plan before mutating state.
        mapped_updates = []
        for item in updates:
            for attribute_key in _mapping_keys(config, item["requirement_id"], lead_source=locked.lead_source, channel="whatsapp"):
                mapped_updates.append(
                    {"key": attribute_key, "value": _mapped_value(config, attribute_key, item["value"], lead_source=locked.lead_source, channel="whatsapp")}
                )
        # A repeated authored mapping to the same key is harmless; keep only the
        # final value for that key in this turn.
        mapped_updates = list({
            str(item["key"]): item
            for item in mapped_updates
            if str(item.get("key") or "").strip()
        }.values())
        attribute_action = ({"type": "attribute_updates", "updates": mapped_updates}
                            if mapped_updates else None)
        completion_reached = _norm(projected.get("qualification_status")) == "completed"
        target = (
            _completion_target(lead=locked, state=projected, config=config)
            if completion_reached
            else None
        )
        runtime_config = {**config, "completion_stage": target}
        stage_action = (
            {
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": str(target["id"])},
            }
            if completion_reached and isinstance(target, dict)
            else None
        )
        reminder_actions = (
            _configured_completion_reminders(runtime_config)
            if completion_reached
            else []
        )
        execution_plan = {
            "qualification_update": deepcopy(update),
            "qualification_updates": deepcopy(updates),
            "attribute_action": deepcopy(attribute_action),
            "stage_action": deepcopy(stage_action),
            "reminder_actions": deepcopy(reminder_actions),
        }

        results = deepcopy(config["errors"])
        action_types = ["qualification_state"]

        _persist_updates_against_requirements(
            lead=locked,
            requirements=requirements,
            updates=updates,
        )
        locked.refresh_from_db(fields=["attributes", "pipeline", "stage"])

        if attribute_action:
            try:
                CRMActionExecutor().execute(
                    organization=organization,
                    lead=locked,
                    actions=[attribute_action],
                )
                for mapped in mapped_updates:
                    verified_attribute = _verify_attribute(locked, mapped["key"], mapped["value"])
                    results.append(verified_attribute)
                if all(item.get("verified") for item in results if item.get("type") == "attribute_updates"):
                    action_types.append("attribute_updates")
            except Exception as exc:
                results.append(
                    {
                        "type": "attribute_updates",
                        "status": "failed",
                        "verified": False,
                        "code": "attribute_persistence_failed",
                        "detail": str(exc)[:500],
                        "updates": [{"key": item["key"], "expected": item["value"], "actual": None}
                                    for item in mapped_updates],
                    }
                )

        # Attribute-presence criteria must see the committed mapped values from
        # this answer, including the last required answer in the questionnaire.
        if completion_reached:
            target = _completion_target(lead=locked, state=projected, config=config)
            runtime_config["completion_stage"] = target
            stage_action = ({"type": "pipeline_transition", "stage_shift": {"stage_id": str(target["id"])}}
                            if isinstance(target, dict) else None)
            execution_plan["stage_action"] = deepcopy(stage_action)

        # Stage completion is an independent configured downstream action. A
        # failed/missing mapped attribute is reported, but does not silently erase
        # a separately configured completion-stage action.
        if stage_action:
            try:
                CRMActionExecutor().execute(
                    organization=organization,
                    lead=locked,
                    actions=[stage_action],
                )
                verified_stage = _verify_stage(locked, target)
                results.append(verified_stage)
                if verified_stage["verified"]:
                    action_types.append("pipeline_transition")
            except Exception as exc:
                locked.refresh_from_db(fields=["pipeline", "stage"])
                results.append(
                    {
                        "type": "pipeline_transition",
                        "status": "failed",
                        "verified": False,
                        "code": "stage_transition_failed",
                        "detail": str(exc)[:500],
                        "target_stage_id": str(target["id"]),
                        "target_pipeline_id": str(target["pipeline_id"]),
                        "actual_stage_id": str(locked.stage_id or ""),
                        "actual_pipeline_id": str(locked.pipeline_id or ""),
                    }
                )

        # Completion reminders are created only from explicit authored reminder
        # rules with a resolvable due time. Qualification completion alone never
        # invents a reminder.
        for reminder_action in reminder_actions:
            try:
                reminder_result = CRMActionExecutor().execute(
                    organization=organization,
                    lead=locked,
                    actions=[reminder_action],
                )
                results.extend(reminder_result)
                if reminder_result:
                    action_types.append("create_reminder")
            except Exception as exc:
                results.append(
                    {
                        "type": "create_reminder",
                        "status": "failed",
                        "verified": False,
                        "code": "reminder_creation_failed",
                        "detail": str(exc)[:500],
                    }
                )

        locked.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        final_state = qs.state_for_lead(
            locked,
            requirements=requirements,
        )
        plan = _plan(
            source=source,
            answer=answer,
            state=final_state,
            requirements=requirements,
            config=runtime_config,
            results=results,
        )

        _mark_state_resolved(
            lead=locked,
            inbound=source,
            action_types=action_types,
        )
        source.refresh_from_db(fields=["raw_payload"])
        processing = _processing(source)
        processing[_CONTRACT_KEY] = {
            "applied": True,
            "applied_at": timezone.now().isoformat(),
            "requirement_id": active_id,
            "normalized_answer": answer,
            "confidence": confidence,
            "execution_plan": execution_plan,
            "configuration_errors": deepcopy(config["errors"]),
        }
        processing[_PLAN_KEY] = deepcopy(plan)
        _save_processing(source, processing)

        reconciler = StateReconciler()
        snapshot = reconciler.build(
            lead=locked,
            source_message_id=source.id,
            execution_results=results,
            structured_decision={
                "intent": "qualification_answer",
                "source_message_id": str(source.id),
                "qualification_updates": deepcopy(updates),
                "attribute_updates": (
                    deepcopy(attribute_action["updates"])
                    if attribute_action
                    else []
                ),
                "workflow_actions": [
                    *([deepcopy(stage_action)] if stage_action else []),
                    *deepcopy(reminder_actions),
                ],
            },
        )
        snapshot["response_plan"] = deepcopy(plan)
        reconciler.persist_for_source(
            lead=locked,
            source_message_id=source.id,
            snapshot=snapshot,
        )
        return {
            "applied": True,
            "qualification_status": final_state.get("qualification_status"),
            "response_plan": plan,
            "execution_results": results,
            "stage_id": str(locked.stage_id or ""),
        }
