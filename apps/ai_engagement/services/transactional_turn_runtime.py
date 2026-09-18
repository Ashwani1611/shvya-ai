from __future__ import annotations

import logging
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

from django.db import transaction
from django.utils import timezone


logger = logging.getLogger(__name__)
_INSTALLED = False
_PRECOMPUTED_DECISION: ContextVar[dict[str, Any] | None] = ContextVar(
    "shvya_precomputed_engagement_decision",
    default=None,
)
_PRE_RESOLVED_MESSAGE_KEY = "pre_resolved_message_id"
_PRE_RESOLVED_AT_KEY = "pre_resolved_at"
_PRE_RESOLVED_ACTIONS_KEY = "pre_resolved_actions"


def _latest_inbound(lead, *, account_id=None):
    from apps.channels.models import WhatsAppMessage

    messages = lead.whatsapp_messages.filter(organization=lead.organization)
    if account_id is not None:
        messages = messages.filter(account_id=account_id)
    message = messages.order_by("-created_at", "-id").first()
    if message is None or message.direction != WhatsAppMessage.Direction.INBOUND:
        return None
    return message


def _runtime_state(lead) -> dict[str, Any]:
    from apps.ai_engagement.services.runtime_state import STATE_KEY

    attributes = lead.attributes if isinstance(getattr(lead, "attributes", None), dict) else {}
    value = attributes.get(STATE_KEY)
    return deepcopy(value) if isinstance(value, dict) else {}


def _message_state_resolved(*, lead, source_message_id) -> bool:
    source_id = str(source_message_id or "")
    if not source_id:
        return False
    runtime = _runtime_state(lead)
    if str(runtime.get(_PRE_RESOLVED_MESSAGE_KEY) or "") == source_id:
        return True

    message = (
        lead.whatsapp_messages.filter(
            id=source_message_id,
            organization=lead.organization,
            direction="inbound",
        )
        .only("raw_payload")
        .first()
    )
    if message is None:
        return False
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    processing = payload.get("shvya_ai_processing")
    return bool(
        isinstance(processing, dict)
        and str(processing.get("message_id") or "") == source_id
        and processing.get("state_resolved")
    )


def _state_changing_decision(decision) -> bool:
    if getattr(decision, "qualification_updates", []) or []:
        return True
    for action in getattr(decision, "crm_actions", []) or []:
        if isinstance(action, dict) and action.get("type") in {
            "attribute_updates",
            "pipeline_transition",
            "create_reminder",
            "contact_updates",
            "add_note",
        }:
            return True
    return False


def _split_actions(actions):
    attributes: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type == "attribute_updates":
            attributes.append(deepcopy(action))
        elif action_type == "pipeline_transition":
            stages.append(deepcopy(action))
        else:
            other.append(deepcopy(action))
    return attributes, stages, other


def _requirements_for_turn(*, organization, lead):
    """Compile the same organization-specific flow used by live engagement.

    The profile compiler is intentionally used instead of directly parsing only
    OrgInfo.qualification_requirements because Engagement Instructions may be the
    configured qualification source for an organization.
    """
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.organization_profile import compile_org_ai_profile
    from apps.ai_engagement.services.qualification_state import requirements_for_lead

    org_info = OrgInfo.objects.filter(organization=organization).first()
    profile = compile_org_ai_profile(
        organization_name=str(getattr(organization, "name", "") or ""),
        org_info=org_info,
    )
    configured = (profile.get("qualification") or {}).get("requirements") or []
    return requirements_for_lead(lead, configured)


def _persist_updates_against_requirements(*, lead, requirements, updates):
    """Persist validated qualification updates against the pinned effective flow."""
    from apps.ai_engagement.services import qualification_state as state_module

    if not updates:
        return state_module.state_for_lead(lead, requirements=requirements)

    source_ids = [
        item.get("source_message_id")
        for item in updates
        if isinstance(item, dict) and item.get("source_message_id")
    ]
    messages = list(
        lead.whatsapp_messages.filter(
            organization_id=lead.organization_id,
            id__in=source_ids,
            direction="inbound",
        ).values("id", "body", "direction")
    )
    base = state_module.state_for_lead(lead, requirements=requirements)
    if not base.get("flow_snapshot") and requirements:
        base["flow_snapshot"] = state_module._snapshot(requirements)
        base["flow_version"] = state_module._flow_version(requirements)
    before_completed = base.get("qualification_status") == state_module.STATUS_COMPLETED
    projected = state_module.project_answer_updates(
        state=base,
        requirements=requirements,
        updates=updates,
        messages=messages,
    )
    if projected.get("qualification_status") == state_module.STATUS_COMPLETED and not before_completed:
        state_module._append_history(projected, event="qualification_answers_complete")
    return state_module._persist_state(lead, projected)


def _persist_qualification_updates(*, lead, decision, requirements):
    """Persist evidence-backed answers without marking the outbound turn complete."""
    updates = getattr(decision, "qualification_updates", []) or []
    state = _persist_updates_against_requirements(
        lead=lead,
        requirements=requirements,
        updates=updates,
    )
    if updates:
        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
    return state


def _qualified_action(*, lead, qualification_state) -> dict[str, Any] | None:
    """Qualified is a backend completion transition, never a model judgement."""
    from apps.ai_engagement.services.qualification_state import normalize_stage_name

    if str(qualification_state.get("qualification_status") or "").casefold() != "completed":
        return None
    if normalize_stage_name(getattr(getattr(lead, "stage", None), "name", "")) != "new lead":
        return None
    stage_id = str(qualification_state.get("qualified_stage_id") or "").strip()
    if not stage_id:
        return None
    return {"type": "pipeline_transition", "stage_shift": {"stage_id": stage_id}}


def _mark_state_resolved(*, lead, inbound, action_types: list[str]) -> None:
    from apps.ai_engagement.services.runtime_state import STATE_KEY, observe_message

    lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
    attributes = deepcopy(lead.attributes) if isinstance(lead.attributes, dict) else {}
    runtime = observe_message(attributes.get(STATE_KEY), inbound.body)
    runtime[_PRE_RESOLVED_MESSAGE_KEY] = str(inbound.id)
    runtime[_PRE_RESOLVED_AT_KEY] = timezone.now().isoformat()
    runtime[_PRE_RESOLVED_ACTIONS_KEY] = list(dict.fromkeys(action_types))[:20]
    attributes[STATE_KEY] = runtime
    lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
    lead.attributes = attributes

    payload = deepcopy(inbound.raw_payload) if isinstance(inbound.raw_payload, dict) else {}
    processing = payload.get("shvya_ai_processing")
    processing = deepcopy(processing) if isinstance(processing, dict) else {}
    processing.update(
        {
            "message_id": str(inbound.id),
            "state_resolved": True,
            "state_resolved_at": runtime[_PRE_RESOLVED_AT_KEY],
        }
    )
    payload["shvya_ai_processing"] = processing
    inbound.raw_payload = payload
    inbound.save(update_fields=["raw_payload", "updated_at"])


def _resolve_state_before_response(
    *,
    organization,
    lead,
    source_message_id,
    decision,
    account_id=None,
) -> dict[str, Any]:
    """Apply one inbound turn in deterministic mutation order.

    Order inside the lead row lock:
      attribute updates -> qualification state -> completion/Qualified stage ->
      other validated workflow actions -> state-resolved marker.

    Response generation happens only after this transaction commits, so the
    response generator receives the resulting CRM/qualification/stage state.
    """
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor
    from apps.ai_engagement.services.qualification_state import state_for_lead
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead

    with transaction.atomic():
        locked_lead = (
            Lead.objects.select_for_update()
            .select_related("organization", "pipeline", "stage")
            .get(pk=lead.pk, organization=organization)
        )
        inbound_query = WhatsAppMessage.objects.select_for_update().filter(
            pk=source_message_id,
            organization=organization,
            lead=locked_lead,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        if account_id is not None:
            inbound_query = inbound_query.filter(account_id=account_id)
        inbound = inbound_query.get()
        latest = _latest_inbound(locked_lead, account_id=account_id)
        if latest is None or latest.pk != inbound.pk:
            return {"applied": False, "reason": "conversation_changed"}

        payload = inbound.raw_payload if isinstance(inbound.raw_payload, dict) else {}
        processing = payload.get("shvya_ai_processing")
        if isinstance(processing, dict) and processing.get("processed"):
            return {"applied": False, "reason": "message_already_processed"}
        if _message_state_resolved(lead=locked_lead, source_message_id=inbound.pk):
            return {"applied": False, "reason": "state_already_resolved"}

        requirements = _requirements_for_turn(
            organization=organization,
            lead=locked_lead,
        )
        attribute_actions, proposed_stage_actions, other_actions = _split_actions(
            getattr(decision, "crm_actions", []) or []
        )
        executor = CRMActionExecutor()
        executed_types: list[str] = []
        results: list[dict[str, Any]] = []

        if attribute_actions:
            results.extend(
                executor.execute(
                    organization=organization,
                    lead=locked_lead,
                    actions=attribute_actions,
                )
            )
            executed_types.append("attribute_updates")
            locked_lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])

        qualification_state = _persist_qualification_updates(
            lead=locked_lead,
            decision=decision,
            requirements=requirements,
        )
        locked_lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        qualification_state = state_for_lead(
            locked_lead,
            requirements=requirements,
        )
        if getattr(decision, "qualification_updates", []) or []:
            executed_types.append("qualification_state")

        completion_stage = _qualified_action(
            lead=locked_lead,
            qualification_state=qualification_state,
        )
        if str(qualification_state.get("qualification_status") or "").casefold() == "completed":
            stage_actions = [completion_stage] if completion_stage else []
        else:
            stage_actions = proposed_stage_actions[:1]
        if stage_actions:
            results.extend(
                executor.execute(
                    organization=organization,
                    lead=locked_lead,
                    actions=stage_actions,
                )
            )
            executed_types.append("pipeline_transition")
            locked_lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])

        if other_actions:
            results.extend(
                executor.execute(
                    organization=organization,
                    lead=locked_lead,
                    actions=other_actions,
                )
            )
            executed_types.extend(
                str(action.get("type") or "") for action in other_actions
            )
            locked_lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])

        _mark_state_resolved(
            lead=locked_lead,
            inbound=inbound,
            action_types=executed_types,
        )

        return {
            "applied": True,
            "results": results,
            "qualification_status": qualification_state.get("qualification_status"),
            "stage_id": str(getattr(locked_lead, "stage_id", "") or ""),
        }


def _persist_engagement_answers_effective(lead, decision, source_message_id):
    """Finalize a response against the same effective qualification flow."""
    from apps.ai_engagement.services.qualification_state import (
        project_answer_updates,
        state_for_lead,
    )
    from apps.ai_engagement.services.runtime_state import (
        STATE_KEY,
        contract,
        finalize_runtime,
        observe_message,
        response_hash,
        state_revision,
        validate_response,
    )

    inbound = lead.whatsapp_messages.select_for_update().get(
        pk=source_message_id,
        organization_id=lead.organization_id,
        direction="inbound",
    )
    payload = dict(inbound.raw_payload or {})
    if (payload.get("shvya_ai_processing") or {}).get("processed"):
        return False

    requirements = _requirements_for_turn(
        organization=lead.organization,
        lead=lead,
    )
    if getattr(decision, "backend_revision", "") and decision.backend_revision != state_revision(lead):
        raise ValueError("Backend state changed during response generation; retry required.")
    if (
        getattr(decision, "flow_version", "")
        and decision.flow_version != contract(qualification={}, requirements=requirements)["flow_version"]
    ):
        raise ValueError("Qualification flow changed during response generation; retry required.")

    lead.attributes = dict(lead.attributes or {})
    lead.attributes[STATE_KEY] = observe_message(
        lead.attributes.get(STATE_KEY),
        inbound.body,
    )
    updates = getattr(decision, "qualification_updates", []) or []
    projected = project_answer_updates(
        state=state_for_lead(lead, requirements=requirements),
        requirements=requirements,
        updates=updates,
        messages=[
            {
                "id": str(inbound.id),
                "body": inbound.body,
                "direction": "inbound",
            }
        ],
    )
    validate_response(
        decision=decision,
        requirements=requirements,
        runtime=contract(
            qualification=projected,
            requirements=requirements,
            saved=(lead.attributes or {}).get(STATE_KEY),
            organization_id=lead.organization_id,
        ),
    )
    _persist_updates_against_requirements(
        lead=lead,
        requirements=requirements,
        updates=updates,
    )
    lead.attributes = dict(lead.attributes or {})
    lead.attributes[STATE_KEY] = observe_message(
        lead.attributes.get(STATE_KEY),
        inbound.body,
    )
    finalize_runtime(
        lead=lead,
        decision=decision,
        qualification=projected,
        requirements=requirements,
        message_id=source_message_id,
    )
    payload["shvya_ai_processing"] = {
        "message_id": str(source_message_id),
        "processed": True,
        "response_hash": response_hash(decision.message),
    }
    inbound.raw_payload = payload
    inbound.save(update_fields=["raw_payload", "updated_at"])
    from apps.ai_engagement.services.lead_intelligence import observe_accepted_turn
    observe_accepted_turn(lead=lead, source_message_id=source_message_id)
    return True


def _suppress_already_resolved_actions(current_builder):
    """Final response generation must not repeat pre-resolved CRM writes."""

    def build(*, decision, context, runtime_policy, qualification_state, requirements):
        controlled, result = current_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )
        conversation = getattr(context, "conversation", None)
        messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
        latest_id = ""
        for message in reversed(messages or []):
            if isinstance(message, dict) and message.get("direction") == "inbound":
                latest_id = str(message.get("id") or "").strip()
                if latest_id:
                    break
        lead_context = getattr(context, "lead", None)
        lead_context = lead_context if isinstance(lead_context, dict) else {}
        attributes = lead_context.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        from apps.ai_engagement.services.runtime_state import STATE_KEY

        runtime = attributes.get(STATE_KEY)
        runtime = runtime if isinstance(runtime, dict) else {}
        if latest_id and str(runtime.get(_PRE_RESOLVED_MESSAGE_KEY) or "") == latest_id:
            return [], {**(result or {}), "state_pre_resolved": True}
        return controlled, result

    return build


def _cache_decision_for_canonical_pass(*, lead, decision, state_revision):
    return _PRECOMPUTED_DECISION.set(
        {
            "lead_id": str(lead.pk),
            "revision": state_revision(lead),
            "decision": decision,
        }
    )


def install_transactional_turn_runtime() -> None:
    """Resolve CRM state before API/Hosted tasks build their final response."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement import tasks as task_module
    from apps.ai_engagement.graph import policy_actions as policy_actions_module
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.ai_engagement.services.engagement import EngagementService
    from apps.ai_engagement.services.runtime_state import state_revision
    from apps.crm.models import Lead
    from services.channels.whatsapp_service import resolve_account_for_lead

    policy_actions_module.build_controlled_actions = _suppress_already_resolved_actions(
        policy_actions_module.build_controlled_actions
    )

    current_engage = EngagementService.engage

    def cached_engage(self, *, organization, lead, knowledge_query=None, context=None):
        cached = _PRECOMPUTED_DECISION.get()
        if (
            isinstance(cached, dict)
            and str(cached.get("lead_id") or "") == str(getattr(lead, "id", ""))
            and str(cached.get("revision") or "") == state_revision(lead)
        ):
            _PRECOMPUTED_DECISION.set(None)
            return cached["decision"]
        return current_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )

    EngagementService.engage = cached_engage
    task_module._persist_engagement_answers = _persist_engagement_answers_effective

    original_execute = task_module._execute_ai_engagement_response_impl

    def transactional_execute(*, task, lead_id: str):
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .filter(pk=lead_id)
            .first()
        )
        if lead is None:
            return original_execute(task=task, lead_id=lead_id)

        source = _latest_inbound(lead)
        if source is None or _message_state_resolved(lead=lead, source_message_id=source.pk):
            return original_execute(task=task, lead_id=lead_id)

        try:
            permission = AIPermissionService().evaluate(
                organization=lead.organization,
                lead=lead,
            )
        except Exception:
            return original_execute(task=task, lead_id=lead_id)
        if not permission.allowed:
            return original_execute(task=task, lead_id=lead_id)
        if resolve_account_for_lead(organization=lead.organization, lead=lead) is None:
            return original_execute(task=task, lead_id=lead_id)

        try:
            draft = EngagementService().engage(
                organization=lead.organization,
                lead=lead,
            )
        except Exception:
            logger.exception(
                "Pre-response state resolution generation failed for lead %s",
                lead_id,
            )
            return original_execute(task=task, lead_id=lead_id)

        if not _state_changing_decision(draft):
            token = _cache_decision_for_canonical_pass(
                lead=lead,
                decision=draft,
                state_revision=state_revision,
            )
            try:
                return original_execute(task=task, lead_id=lead_id)
            finally:
                _PRECOMPUTED_DECISION.reset(token)

        try:
            resolution = _resolve_state_before_response(
                organization=lead.organization,
                lead=lead,
                source_message_id=source.pk,
                decision=draft,
            )
        except Exception:
            logger.exception(
                "Pre-response CRM state resolution failed for lead %s",
                lead_id,
            )
            return original_execute(task=task, lead_id=lead_id)

        if resolution.get("reason") == "conversation_changed":
            return {
                "status": "skipped",
                "reason": "conversation_changed_before_state_resolution",
                "lead_id": str(lead_id),
                "source_message_id": str(source.pk),
            }
        return original_execute(task=task, lead_id=lead_id)

    task_module._execute_ai_engagement_response_impl = transactional_execute

    from apps.hosted_automation import execution as hosted_execution

    original_hosted_execute = hosted_execution.execute_hosted_ai_engagement

    def transactional_hosted_execute(*, task, job):
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .filter(pk=job.lead_id, organization_id=job.organization_id)
            .first()
        )
        if lead is None:
            return original_hosted_execute(task=task, job=job)
        source = _latest_inbound(lead, account_id=job.account_id)
        if (
            source is None
            or str(source.pk) != str(job.source_message_id)
            or _message_state_resolved(lead=lead, source_message_id=source.pk)
        ):
            return original_hosted_execute(task=task, job=job)

        account = hosted_execution._connected_hosted_account(
            account_id=job.account_id,
            organization_id=job.organization_id,
        )
        if account is None:
            return original_hosted_execute(task=task, job=job)
        try:
            permission = AIPermissionService().evaluate(
                organization=lead.organization,
                lead=lead,
                latest_inbound=source,
            )
        except Exception:
            return original_hosted_execute(task=task, job=job)
        if not permission.allowed:
            return original_hosted_execute(task=task, job=job)

        service = EngagementService(
            context_builder=hosted_execution.HostedAIContextBuilder(account_id=account.id),
        )
        try:
            draft = service.engage(
                organization=lead.organization,
                lead=lead,
            )
        except Exception:
            logger.exception(
                "Hosted pre-response state resolution generation failed for lead %s",
                lead.pk,
            )
            return original_hosted_execute(task=task, job=job)

        if not _state_changing_decision(draft):
            token = _cache_decision_for_canonical_pass(
                lead=lead,
                decision=draft,
                state_revision=state_revision,
            )
            try:
                return original_hosted_execute(task=task, job=job)
            finally:
                _PRECOMPUTED_DECISION.reset(token)

        try:
            resolution = _resolve_state_before_response(
                organization=lead.organization,
                lead=lead,
                source_message_id=source.pk,
                decision=draft,
                account_id=account.id,
            )
        except Exception:
            logger.exception(
                "Hosted pre-response CRM state resolution failed for lead %s",
                lead.pk,
            )
            return original_hosted_execute(task=task, job=job)

        if resolution.get("reason") == "conversation_changed":
            return {
                "status": "skipped",
                "reason": "conversation_changed_before_state_resolution",
                "lead_id": str(lead.pk),
                "source_message_id": str(source.pk),
            }
        return original_hosted_execute(task=task, job=job)

    hosted_execution.execute_hosted_ai_engagement = transactional_hosted_execute
    _INSTALLED = True
