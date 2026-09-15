from __future__ import annotations

from copy import deepcopy

from django.db import transaction
from django.utils import timezone


_INSTALLED = False
_TERMINAL = {"answered", "skipped", "not_applicable"}


def _is_persistent_model_instance(value) -> bool:
    """Return whether value is a saved Django model instance."""
    return bool(
        value is not None
        and getattr(value, "_meta", None) is not None
        and getattr(value, "pk", None) is not None
    )


def reconcile_lead_qualification_from_attributes(*, organization, lead):
    """Reconcile existing CRM values only through explicit configured mappings.

    This utility intentionally has no semantic/fuzzy attribute matcher. A CRM
    value can satisfy a requirement only when AI Brain configuration explicitly
    maps that requirement to that attribute. Completion-stage execution likewise
    uses the configured Stage Shifting rule rather than a magic stage name.
    Existing answered state is never overwritten.
    """
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.organization_profile import compile_org_ai_profile
    from apps.ai_engagement.services import qualification_state as state_module
    from apps.ai_engagement.services.qualification_execution_contract import _config
    from apps.crm.models import Lead

    if not _is_persistent_model_instance(organization) or not _is_persistent_model_instance(lead):
        return None

    with transaction.atomic():
        locked = (
            Lead.objects.select_for_update()
            .select_related("organization", "pipeline", "stage")
            .get(pk=lead.pk, organization=organization)
        )
        if state_module.normalize_stage_name(getattr(locked.stage, "name", "")) != "new lead":
            return state_module.state_for_lead(locked)

        org_info = OrgInfo.objects.filter(organization=organization).first()
        profile = compile_org_ai_profile(
            organization_name=str(getattr(organization, "name", "") or ""),
            org_info=org_info,
        )
        configured = (profile.get("qualification") or {}).get("requirements") or []
        requirements = state_module.requirements_for_lead(locked, configured)
        if not requirements:
            return state_module.state_for_lead(locked, requirements=[])

        contract_config = _config(
            organization=organization,
            requirements=requirements,
        )
        mappings = contract_config.get("mappings") or {}
        attributes = deepcopy(locked.attributes) if isinstance(locked.attributes, dict) else {}
        state = state_module.state_for_lead(locked, requirements=requirements)
        states = deepcopy(state.get("requirement_states") or {})
        changed = False

        for requirement in sorted(
            requirements,
            key=lambda item: (int(item.get("priority") or 999999), str(item.get("id") or "")),
        ):
            requirement_id = str(requirement.get("id") or "").strip()
            if not requirement_id or not requirement.get("required", True):
                continue
            existing = states.get(requirement_id) or {}
            if str(existing.get("status") or "").casefold() in _TERMINAL:
                continue

            key = str(mappings.get(requirement_id) or "").strip()
            if not key:
                continue
            value = attributes.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue

            now = timezone.now().isoformat()
            states[requirement_id] = {
                **state_module._requirement_state(existing),
                "status": state_module.REQUIREMENT_ANSWERED,
                "value": value,
                "raw_answer": value,
                "confidence": "verified_crm",
                "source_message_id": None,
                "updated_at": now,
            }
            state_module._append_history(
                state,
                event="answered_from_existing_attribute",
                requirement_id=requirement_id,
                value=value,
            )
            changed = True

        if not changed:
            return state

        state["requirement_states"] = states
        state["missing_requirement_ids"] = state_module._missing_ids(requirements, states)
        state["answered_requirement_ids"] = state_module._answered_ids(requirements, states)
        state["qualification_answers"] = state_module._answers(requirements, states)
        state["all_requirements_answered"] = bool(requirements) and not state["missing_requirement_ids"]
        if not state.get("flow_snapshot"):
            state["flow_snapshot"] = state_module._snapshot(requirements)
            state["flow_version"] = state_module._flow_version(requirements)

        if state["all_requirements_answered"]:
            state["qualification_status"] = state_module.STATUS_COMPLETED
            state["qualification_completed"] = True
            state["qualification_completed_at"] = (
                state.get("qualification_completed_at") or timezone.now().isoformat()
            )
            state["current_requirement_id"] = None
            state["next_requirement_id"] = None
            state["engagement_mode"] = state_module.MODE_CONVERSATION
            state_module._append_history(state, event="qualification_answers_complete")
        elif state.get("qualification_status") == state_module.STATUS_NOT_STARTED:
            state["qualification_status"] = state_module.STATUS_IN_PROGRESS

        state_module._persist_state(locked, state)
        locked.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        normalized = state_module.state_for_lead(locked, requirements=requirements)

        target = contract_config.get("completion_stage")
        if (
            normalized.get("qualification_status") == state_module.STATUS_COMPLETED
            and isinstance(target, dict)
            and target.get("id") is not None
        ):
            from apps.ai_engagement.services.crm_executor import CRMActionExecutor

            CRMActionExecutor().execute(
                organization=organization,
                lead=locked,
                actions=[
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {"stage_id": str(target["id"])},
                    }
                ],
            )
            locked.refresh_from_db(fields=["attributes", "pipeline", "stage"])
            normalized = state_module.state_for_lead(locked, requirements=requirements)
        return normalized


def install_attribute_state_reconciliation() -> None:
    """Optional pre-generation reconciliation for explicitly mapped CRM values."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService

    current_engage = EngagementService.engage

    def engage(self, *, organization, lead, knowledge_query=None, context=None):
        if _is_persistent_model_instance(organization) and _is_persistent_model_instance(lead):
            reconcile_lead_qualification_from_attributes(
                organization=organization,
                lead=lead,
            )
            try:
                lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
            except (AttributeError, TypeError, ValueError):
                pass
        return current_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )

    EngagementService.engage = engage
    _INSTALLED = True
