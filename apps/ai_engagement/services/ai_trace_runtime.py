from __future__ import annotations

import time

from apps.ai_engagement.services.turn_scope import isolated_ai_turn

_INSTALLED = False


def _elapsed_ms(started: float) -> int:
    return max(int((time.perf_counter() - started) * 1000), 0)


def _safe_requirement_state(context):
    lead_data = getattr(context, "lead", None) or {}
    state = lead_data.get("qualification") if isinstance(lead_data, dict) else {}
    if not isinstance(state, dict):
        return {}
    return {
        "qualification_status": state.get("qualification_status"),
        "qualification_result": state.get("qualification_result"),
        "engagement_mode": state.get("engagement_mode"),
        "next_requirement_id": state.get("next_requirement_id"),
        "last_asked_requirement_id": state.get("last_asked_requirement_id"),
        "flow_version": state.get("flow_version"),
        "requirement_states": state.get("requirement_states") or {},
    }


def _permission_snapshot(*, organization, lead, decision):
    pipeline = getattr(lead, "pipeline", None)
    stage = getattr(lead, "stage", None)
    reason = str(getattr(decision, "reason", "") or "")
    allowed = bool(getattr(decision, "allowed", False))
    return {
        "organization_id": str(getattr(organization, "id", "") or ""),
        "organization_ai_enabled": reason != "organization_ai_disabled",
        "pipeline_ai_enabled": bool(getattr(pipeline, "ai_enabled", True)),
        "stage_ai_enabled": bool(getattr(stage, "ai_on", True)),
        "lead_ai_enabled": bool(getattr(lead, "ai_enabled", False)),
        "account_valid": (
            not reason.startswith("whatsapp_account_") if not allowed else True
        ),
        "pipeline_account_mapping_valid": reason not in {
            "pipeline_whatsapp_number_missing",
            "pipeline_whatsapp_account_mismatch",
        },
        "final_permission_result": allowed,
        "reason": reason,
    }


def _start_api_trace(lead_id):
    from apps.ai_engagement.services.trace_service import begin_trace, record
    from apps.crm.models import Lead

    lead = (
        Lead.objects.select_related("organization", "pipeline", "stage")
        .filter(pk=lead_id)
        .first()
    )
    if lead is None:
        return None
    source = (
        lead.whatsapp_messages.filter(
            organization=lead.organization,
            direction="inbound",
        )
        .select_related("account")
        .order_by("-created_at", "-id")
        .first()
    )
    account = getattr(source, "account", None)
    token = begin_trace(
        organization=lead.organization,
        lead=lead,
        source_message=source,
        account=account,
    )
    record(
        "identity",
        {
            "organization_id": str(lead.organization_id),
            "lead_id": str(lead.id),
            "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else None,
            "stage_id": str(lead.stage_id) if lead.stage_id else None,
            "whatsapp_account_id": (
                str(getattr(account, "id", "") or "") or None
            ),
            "source_inbound_message_id": (
                str(getattr(source, "id", "") or "") or None
            ),
            "connection_type": str(
                getattr(account, "connection_type", "") or ""
            ),
        },
    )
    return token


def _start_hosted_trace(job):
    from apps.ai_engagement.services.trace_service import begin_trace, record
    from apps.channels.models import WhatsAppAccount, WhatsAppMessage
    from apps.crm.models import Lead

    lead = (
        Lead.objects.select_related("organization", "pipeline", "stage")
        .filter(pk=job.lead_id, organization_id=job.organization_id)
        .first()
    )
    if lead is None:
        return None
    account = WhatsAppAccount.objects.filter(
        pk=job.account_id,
        organization_id=job.organization_id,
    ).first()
    source = WhatsAppMessage.objects.filter(
        pk=job.source_message_id,
        organization_id=job.organization_id,
        lead_id=job.lead_id,
        account_id=job.account_id,
        direction="inbound",
    ).first()
    token = begin_trace(
        organization=lead.organization,
        lead=lead,
        source_message=source,
        account=account,
    )
    record(
        "identity",
        {
            "organization_id": str(lead.organization_id),
            "lead_id": str(lead.id),
            "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else None,
            "stage_id": str(lead.stage_id) if lead.stage_id else None,
            "whatsapp_account_id": (
                str(getattr(account, "id", "") or "") or None
            ),
            "source_inbound_message_id": (
                str(getattr(source, "id", "") or "") or None
            ),
            "connection_type": str(
                getattr(account, "connection_type", "") or ""
            ),
            "hosted_job_id": str(getattr(job, "id", "") or "") or None,
        },
    )
    return token


def install_ai_trace_runtime():
    """Observe final production runtimes without owning policy or mutations."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from apps.ai_engagement.services.trace_service import (
        append,
        finalize_from_result,
        flush,
        mark_decision,
        mark_error,
        record,
    )

    from apps.ai_engagement.services import ai_permissions as permission_module

    original_permission = permission_module.AIPermissionService.evaluate

    def traced_permission(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            decision = original_permission(self, *args, **kwargs)
        except Exception as exc:
            mark_error(
                step="ai_permission",
                exc=exc,
                code="AI_PERMISSION_EVALUATION_FAILED",
            )
            record("performance", {"permission_ms": _elapsed_ms(started)})
            raise
        record(
            "permission",
            _permission_snapshot(
                organization=kwargs.get("organization"),
                lead=kwargs.get("lead"),
                decision=decision,
            ),
        )
        record("performance", {"permission_ms": _elapsed_ms(started)})
        return decision

    permission_module.AIPermissionService.evaluate = traced_permission

    from apps.ai_engagement.services import context as context_module

    original_context_build = context_module.AIContextBuilder.build

    def traced_context_build(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            context = original_context_build(self, *args, **kwargs)
        except Exception as exc:
            mark_error(
                step="context_build",
                exc=exc,
                code="CONTEXT_BUILD_FAILED",
            )
            record("performance", {"context_build_ms": _elapsed_ms(started)})
            raise

        record(
            "context",
            {
                "organization_id": (context.organization or {}).get("id"),
                "lead_id": (context.lead or {}).get("id"),
                "pipeline_id": (context.pipeline or {}).get("id"),
                "stage_id": (context.stage or {}).get("id"),
                "conversation_message_count": (
                    context.conversation or {}
                ).get("message_count", 0),
            },
        )
        qualification = _safe_requirement_state(context)
        if qualification:
            record("qualification", {"state_before_generation": qualification})

        query = str(kwargs.get("knowledge_query") or "").strip()
        invoked = bool(query or kwargs.get("query_vector") is not None)
        if invoked:
            knowledge = list(getattr(context, "knowledge", []) or [])
            references = [
                {
                    "document_id": item.get("document_id"),
                    "chunk_id": item.get("chunk_id"),
                    "score": item.get("similarity"),
                    "distance": item.get("distance"),
                }
                for item in knowledge
                if isinstance(item, dict)
            ]
            record(
                "rag",
                {
                    "invoked": True,
                    "reason": "existing_engagement_knowledge_decision",
                    "query": query or "[precomputed vector]",
                    "retrieval_path": "SEMANTIC" if references else "NONE",
                    "retrieved": references,
                    "chunk_count": len(references),
                },
            )
            record(
                "performance",
                {"rag_context_build_ms": _elapsed_ms(started)},
            )
        record("performance", {"context_build_ms": _elapsed_ms(started)})
        return context

    context_module.AIContextBuilder.build = traced_context_build

    from apps.ai_engagement.services import engagement as engagement_module

    original_engage = engagement_module.EngagementService.engage

    def traced_engage(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            decision = original_engage(self, *args, **kwargs)
        except Exception as exc:
            cause = getattr(exc, "__cause__", None)
            mark_error(
                step="generation",
                exc=exc,
                retryable=bool(
                    getattr(exc, "retryable", False)
                    or getattr(cause, "retryable", False)
                ),
                code="ENGAGEMENT_GENERATION_FAILED",
            )
            record(
                "performance",
                {"model_or_deterministic_ms": _elapsed_ms(started)},
            )
            raise
        mark_decision(decision=decision)
        # This is a read-only proposal observation. File delivery and all CRM
        # mutations still use their existing canonical validation boundaries.
        from apps.ai_engagement.services.action_planner import ActionPlanner
        from apps.ai_engagement.services.action_plan_trace import trace_action_plan
        from apps.ai_engagement.services.tenant_guard import TenantScopeError
        organization, lead = kwargs.get("organization"), kwargs.get("lead")
        from apps.crm.models import Lead
        # Sandbox previews intentionally use synthetic leads and have no durable
        # CRM action context. Their existing preview validator remains authoritative.
        # Only persisted CRM leads enter this read-only production observation.
        if organization is not None and isinstance(lead, Lead) and not lead._state.adding:
            try:
                trace_action_plan(ActionPlanner().plan(organization=organization, lead=lead, decision=decision))
            except TenantScopeError:
                raise
            except Exception as exc:
                mark_error(step="action_plan_observation", exc=exc, code="ACTION_PLAN_OBSERVATION_FAILED")
        record(
            "performance",
            {"model_or_deterministic_ms": _elapsed_ms(started)},
        )
        return decision

    engagement_module.EngagementService.engage = traced_engage

    from apps.ai_engagement.services import crm_executor as crm_module

    original_crm_execute = crm_module.CRMActionExecutor.execute

    def traced_crm_execute(self, *args, **kwargs):
        started = time.perf_counter()
        proposed = kwargs.get("actions") or []
        try:
            result = original_crm_execute(self, *args, **kwargs)
        except Exception as exc:
            append(
                "crm_actions",
                "attempts",
                {
                    "proposed": proposed,
                    "accepted": [],
                    "rejected": proposed,
                    "rejection_reason": exc.__class__.__name__,
                },
            )
            mark_error(
                step="crm_actions",
                exc=exc,
                code="CRM_ACTION_FAILED",
            )
            record("performance", {"crm_action_ms": _elapsed_ms(started)})
            raise
        append(
            "crm_actions",
            "attempts",
            {"proposed": proposed, "accepted": result, "rejected": []},
        )
        record("performance", {"crm_action_ms": _elapsed_ms(started)})
        return result

    crm_module.CRMActionExecutor.execute = traced_crm_execute

    from apps.ai_engagement import tasks as task_module

    original_api_executor = task_module._execute_ai_engagement_response_impl

    @isolated_ai_turn
    def traced_api_executor(*, task, lead_id):
        token = _start_api_trace(lead_id)
        started = time.perf_counter()
        try:
            result = original_api_executor(task=task, lead_id=lead_id)
            finalize_from_result(result)
            record("performance", {"finalization_ms": _elapsed_ms(started)})
            return result
        except Exception as exc:
            mark_error(
                step="api_runtime",
                exc=exc,
                retryable=bool(getattr(exc, "retryable", False)),
                code="API_RUNTIME_EXCEPTION",
            )
            record("finalization", {"decision_accepted": False})
            from apps.ai_engagement.services.trace_service import current

            buffer = current()
            if buffer is not None:
                buffer.data["status"] = "failed"
                buffer.data["reason_code"] = "API_RUNTIME_EXCEPTION"
            raise
        finally:
            flush(reset_token=token)

    task_module._execute_ai_engagement_response_impl = traced_api_executor

    from apps.hosted_automation import execution as hosted_module

    original_hosted_fallback = hosted_module.build_deterministic_fallback_decision

    def traced_hosted_fallback(*args, **kwargs):
        decision = original_hosted_fallback(*args, **kwargs)
        mark_decision(decision=decision)
        record("generation", {"execution_path": "FALLBACK"})
        return decision

    hosted_module.build_deterministic_fallback_decision = traced_hosted_fallback
    original_hosted_executor = hosted_module.execute_hosted_ai_engagement

    @isolated_ai_turn
    def traced_hosted_executor(*, task, job):
        token = _start_hosted_trace(job)
        started = time.perf_counter()
        try:
            result = original_hosted_executor(task=task, job=job)
            finalize_from_result(result)
            record("finalization", {"delivery_owner": "hosted_automation_job"})
            record(
                "performance",
                {"hosted_finalization_ms": _elapsed_ms(started)},
            )
            return result
        except Exception as exc:
            mark_error(
                step="hosted_runtime",
                exc=exc,
                retryable=bool(getattr(exc, "retryable", False)),
                code="HOSTED_RUNTIME_EXCEPTION",
            )
            record(
                "finalization",
                {
                    "decision_accepted": False,
                    "delivery_owner": "hosted_automation_job",
                },
            )
            from apps.ai_engagement.services.trace_service import current

            buffer = current()
            if buffer is not None:
                buffer.data["status"] = "failed"
                buffer.data["reason_code"] = "HOSTED_RUNTIME_EXCEPTION"
            raise
        finally:
            flush(reset_token=token)

    hosted_module.execute_hosted_ai_engagement = traced_hosted_executor
