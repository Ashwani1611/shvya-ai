"""Compatibility and runtime-installation boundary for qualification execution.

The state machine remains singular. Focused implementation components live in
services/qualification_execution; this module preserves historical imports and
the dynamic runtime patch surface used by SHVYA bootstrap code.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from functools import wraps

from apps.ai_engagement.services.qualification_execution.common import (
    _ACK_HEADING_ONLY,
    _ACK_LABEL,
    _COMPLETION_RULE,
    _CONTRACT_KEY,
    _GENERIC_ACKS,
    _GREETING_RE,
    _LABEL_ONLY,
    _MAPPING_ERROR_CODES,
    _PLAN_INSTRUCTIONS,
    _PLAN_KEY,
    _QUESTION_FRAGMENT_RE,
    _attribute_ref,
    _attribute_refs,
    _clean,
    _norm,
    _processing,
    _reference,
    _requirement_ref,
    _save_processing,
    _split_mapping,
    _strip_quotes,
)
from apps.ai_engagement.services.qualification_execution.completion import (
    _completion_target,
    _configured_completion_reminders,
)
from apps.ai_engagement.services.qualification_execution.config import (
    _config,
    _mapped_value,
    _mapping_keys,
)
from apps.ai_engagement.services.qualification_execution.evidence import (
    _additional_explicit_updates,
    _asked_requirement,
    _persist_plan_only,
    _verify_attribute,
    _verify_stage,
)
from apps.ai_engagement.services.qualification_execution.executor import (
    resolve_before_generation,
)
from apps.ai_engagement.services.qualification_execution.finalization import (
    _finalize,
    _leading_greeting,
    _stage_success,
)
from apps.ai_engagement.services.qualification_execution.planning import (
    _clarification_plan,
    _plan,
    _render_requirement,
    _requirement_payload,
    _start_plan,
)
from apps.ai_engagement.services.qualification_execution.reconciliation import (
    _ack_from_message,
    _fallback_acknowledgement,
    _plan_from_reconciled,
)


logger = logging.getLogger(__name__)
_INSTALLED = False


def _latest_inbound(lead, account_id=None):
    query = lead.whatsapp_messages.filter(direction="inbound")
    if account_id is not None:
        query = query.filter(account_id=account_id)
    return query.order_by("-created_at", "-id").first()


def install_qualification_execution_contract() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService

    current_input = EngagementService._build_input
    current_instructions = EngagementService._build_instructions

    @wraps(current_input)
    def build_input(self, *, context, **kwargs):
        raw = current_input(self, context=context, **kwargs)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw
        reconciled = payload.get("reconciled_state")
        if isinstance(reconciled, dict) and isinstance(
            reconciled.get("response_plan"),
            dict,
        ):
            payload["response_plan"] = deepcopy(reconciled["response_plan"])
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @wraps(current_instructions)
    def build_instructions(self, *, context, profile=None):
        base = current_instructions(self, context=context, profile=profile)
        return base if _PLAN_INSTRUCTIONS in base else f"{base}\n\n{_PLAN_INSTRUCTIONS}"

    EngagementService._build_input = build_input
    EngagementService._build_instructions = build_instructions

    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.canonical_architecture import (
        ResponseActionValidator,
        StateReconciler,
    )

    current_resolve = runtime._resolve_state_before_response
    reconciler = StateReconciler()

    @wraps(current_resolve)
    def resolve(*, organization, lead, source_message_id, decision, account_id=None):
        result = current_resolve(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            decision=decision,
            account_id=account_id,
        )
        snapshot = result.get("reconciled_state") if isinstance(result, dict) else None
        if not result or not result.get("applied") or not isinstance(snapshot, dict):
            return result

        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        plan = _plan_from_reconciled(
            lead=lead,
            source_message_id=source_message_id,
            snapshot=snapshot,
        )
        if not plan:
            return result
        snapshot = {**snapshot, "response_plan": plan}
        reconciler.persist_for_source(
            lead=lead,
            source_message_id=source_message_id,
            snapshot=snapshot,
        )
        return {**result, "reconciled_state": snapshot}

    runtime._resolve_state_before_response = resolve

    current_validate = ResponseActionValidator.validate

    @wraps(current_validate)
    def validate(self, *, decision, reconciled_state):
        validated = current_validate(
            self,
            decision=decision,
            reconciled_state=reconciled_state,
        )
        return _finalize(
            validated,
            reconciled_state if isinstance(reconciled_state, dict) else {},
        )

    ResponseActionValidator.validate = validate

    from apps.ai_engagement.tasks import engagement as task_module
    from apps.ai_engagement import tasks as task_compat
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.crm.models import Lead
    from services.channels.whatsapp_service import resolve_account_for_lead

    current_task = task_module._execute_ai_engagement_response_impl

    @wraps(current_task)
    def execute(*, task, lead_id: str):
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .filter(pk=lead_id)
            .first()
        )
        if lead is not None:
            source = _latest_inbound(lead)
            try:
                permission = AIPermissionService().evaluate(
                    organization=lead.organization,
                    lead=lead,
                    latest_inbound=source,
                )
            except Exception:
                permission = None
            account = (
                resolve_account_for_lead(
                    organization=lead.organization,
                    lead=lead,
                )
                if permission and permission.allowed
                else None
            )
            if source is not None and account is not None:
                try:
                    resolve_before_generation(
                        organization=lead.organization,
                        lead=lead,
                        source_message_id=source.pk,
                    )
                except Exception:
                    logger.exception(
                        "Qualification pre-generation execution failed for lead %s",
                        lead_id,
                    )
        return current_task(task=task, lead_id=lead_id)

    task_module._execute_ai_engagement_response_impl = execute
    task_compat._execute_ai_engagement_response_impl = execute

    from apps.hosted_automation import execution as hosted_execution

    current_hosted = hosted_execution.execute_hosted_ai_engagement

    @wraps(current_hosted)
    def execute_hosted(*, task, job):
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .filter(
                pk=job.lead_id,
                organization_id=job.organization_id,
            )
            .first()
        )
        if lead is not None:
            source = _latest_inbound(lead, account_id=job.account_id)
            try:
                permission = AIPermissionService().evaluate(
                    organization=lead.organization,
                    lead=lead,
                    latest_inbound=source,
                )
            except Exception:
                permission = None
            account = hosted_execution._connected_hosted_account(
                account_id=job.account_id,
                organization_id=job.organization_id,
            )
            if (
                permission
                and permission.allowed
                and account is not None
                and source is not None
                and str(source.pk) == str(job.source_message_id)
            ):
                try:
                    resolve_before_generation(
                        organization=lead.organization,
                        lead=lead,
                        source_message_id=source.pk,
                        account_id=job.account_id,
                    )
                except Exception:
                    logger.exception(
                        "Hosted qualification pre-generation execution failed for lead %s",
                        job.lead_id,
                    )
        return current_hosted(task=task, job=job)

    hosted_execution.execute_hosted_ai_engagement = execute_hosted
    _INSTALLED = True

