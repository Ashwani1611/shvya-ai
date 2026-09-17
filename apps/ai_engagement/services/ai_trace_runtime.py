from __future__ import annotations

from contextvars import ContextVar

from apps.ai_engagement.services.ai_trace import AITraceRecorder, TraceTimer, sanitize_trace_value

_INSTALLED = False
_CURRENT_TRACE: ContextVar[object | None] = ContextVar("shvya_ai_trace", default=None)


def _trace():
    return _CURRENT_TRACE.get()


def _safe_update(**kwargs):
    AITraceRecorder.update(_trace(), **kwargs)


def _wrap_engagement_service():
    from apps.ai_engagement.services.engagement import EngagementService
    original = EngagementService.engage
    if getattr(original, "_shvya_ai_trace_wrapped", False):
        return

    def traced_engage(self, *args, **kwargs):
        timer = TraceTimer()
        try:
            decision = original(self, *args, **kwargs)
        except Exception as exc:
            _safe_update(merge={"performance": {"model_ms": timer.ms()}, "errors": [{"processing_step": "generation", "error_type": exc.__class__.__name__, "normalized_error_code": "GENERATION_ERROR", "safe_error_message": str(exc)[:500], "retryable": "Transient" in exc.__class__.__name__}]})
            raise
        model = str(getattr(decision, "model", "") or "")
        execution_path = "DETERMINISTIC" if model.casefold() == "deterministic" else "MODEL"
        proposed_actions = sanitize_trace_value(getattr(decision, "crm_actions", []) or [])
        proposed_q = sanitize_trace_value(getattr(decision, "qualification_updates", []) or [])
        _safe_update(
            execution_path=execution_path,
            model_name=model,
            response=getattr(decision, "message", ""),
            reason_code=getattr(decision, "reason_code", "") or getattr(decision, "reason", ""),
            merge={
                "generation": {
                    "execution_path": execution_path,
                    "model": model,
                    "reason_code": getattr(decision, "reason_code", "") or getattr(decision, "reason", ""),
                    "should_engage": bool(getattr(decision, "should_engage", False)),
                    "silence_rule": sanitize_trace_value(getattr(decision, "silence_rule", None)),
                    "generated_response": str(getattr(decision, "message", "") or "")[:2000],
                    "next_requirement_id": getattr(decision, "next_requirement_id", None),
                    "file_document_id": getattr(decision, "file_document_id", None),
                    "backend_revision": getattr(decision, "backend_revision", ""),
                    "flow_version": getattr(decision, "flow_version", ""),
                },
                "qualification": {"qualification_updates_proposed": proposed_q},
                "crm_actions": {"proposed": proposed_actions},
                "performance": {"model_ms": timer.ms()},
            },
        )
        return decision

    traced_engage._shvya_ai_trace_wrapped = True
    EngagementService.engage = traced_engage


def _wrap_context_rag():
    from apps.ai_engagement.services.context import AIContextBuilder
    original = AIContextBuilder._build_knowledge_context
    if getattr(original, "_shvya_ai_trace_wrapped", False):
        return

    def traced_rag(self, *args, **kwargs):
        query = str(kwargs.get("knowledge_query") or "").strip()
        timer = TraceTimer()
        result = original(self, *args, **kwargs)
        invoked = bool(query or kwargs.get("query_vector") is not None)
        chunks = []
        for item in result or []:
            if isinstance(item, dict):
                chunks.append({"document_id": item.get("document_id"), "chunk_id": item.get("chunk_id"), "score": item.get("similarity")})
        _safe_update(merge={"rag": {"invoked": invoked, "reason": "knowledge_query_present" if invoked else "not_requested", "retrieval_query": query[:1000], "retrieval_path": "SEMANTIC" if invoked else "NONE", "chunks": chunks, "chunk_count": len(chunks), "retrieval_ms": timer.ms()}, "performance": {"rag_ms": timer.ms()}})
        return result

    traced_rag._shvya_ai_trace_wrapped = True
    AIContextBuilder._build_knowledge_context = traced_rag


def _wrap_crm_executor():
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor
    original = CRMActionExecutor.execute
    if getattr(original, "_shvya_ai_trace_wrapped", False):
        return

    def traced_execute(self, *args, **kwargs):
        timer = TraceTimer()
        actions = sanitize_trace_value(kwargs.get("actions", []) or [])
        try:
            result = original(self, *args, **kwargs)
        except Exception as exc:
            _safe_update(merge={"crm_actions": {"proposed": actions, "rejected": actions, "rejection_reason": str(exc)[:500]}, "performance": {"crm_action_ms": timer.ms()}})
            raise
        _safe_update(merge={"crm_actions": {"proposed": actions, "accepted": sanitize_trace_value(result or [])}, "performance": {"crm_action_ms": timer.ms()}})
        return result

    traced_execute._shvya_ai_trace_wrapped = True
    CRMActionExecutor.execute = traced_execute


def _wrap_api_executor():
    from apps.ai_engagement import tasks
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead
    original = tasks._execute_ai_engagement_response_impl
    if getattr(original, "_shvya_ai_trace_wrapped", False):
        return

    def traced_execute(*, task, lead_id):
        timer = TraceTimer()
        trace = None
        token = None
        try:
            lead = Lead.objects.select_related("organization", "pipeline", "stage").filter(pk=lead_id).first()
            if lead is not None:
                source = WhatsAppMessage.objects.filter(organization=lead.organization, lead=lead, direction=WhatsAppMessage.Direction.INBOUND).select_related("account").order_by("-created_at", "-id").first()
                if source is not None:
                    trace = AITraceRecorder.start(organization=lead.organization, lead=lead, source_message=source, account=source.account, connection_type=source.account.connection_type)
                    if trace is not None:
                        AITraceRecorder.update(trace, status="processing")
                        token = _CURRENT_TRACE.set(trace)
            result = original(task=task, lead_id=lead_id)
            AITraceRecorder.finish_from_result(trace, result=result, elapsed_ms=timer.ms())
            return result
        except Exception as exc:
            AITraceRecorder.update(trace, status="failed", reason_code="runtime_exception", merge={"errors": [{"processing_step": "runtime", "error_type": exc.__class__.__name__, "normalized_error_code": "RUNTIME_EXCEPTION", "safe_error_message": str(exc)[:500], "retryable": True}], "performance": {"total_ms": timer.ms()}}, completed=True, total_ms=timer.ms())
            raise
        finally:
            if token is not None:
                _CURRENT_TRACE.reset(token)

    traced_execute._shvya_ai_trace_wrapped = True
    tasks._execute_ai_engagement_response_impl = traced_execute


def _wrap_hosted_executor():
    from apps.hosted_automation import execution
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead
    original = execution.execute_hosted_ai_engagement
    if getattr(original, "_shvya_ai_trace_wrapped", False):
        return

    def traced_hosted(*, task, job):
        timer = TraceTimer()
        trace = None
        token = None
        try:
            lead = Lead.objects.select_related("organization", "pipeline", "stage").filter(pk=job.lead_id, organization_id=job.organization_id).first()
            source = None
            if lead is not None:
                source = WhatsAppMessage.objects.select_related("account").filter(pk=job.source_message_id, organization=lead.organization, lead=lead, account_id=job.account_id, direction=WhatsAppMessage.Direction.INBOUND).first()
            if lead is not None and source is not None:
                trace = AITraceRecorder.start(organization=lead.organization, lead=lead, source_message=source, account=source.account, connection_type="hosted")
                if trace is not None:
                    AITraceRecorder.update(trace, status="processing")
                    token = _CURRENT_TRACE.set(trace)
            result = original(task=task, job=job)
            AITraceRecorder.finish_from_result(trace, result=result, elapsed_ms=timer.ms())
            return result
        except Exception as exc:
            AITraceRecorder.update(trace, status="failed", reason_code="runtime_exception", merge={"errors": [{"processing_step": "hosted_runtime", "error_type": exc.__class__.__name__, "normalized_error_code": "RUNTIME_EXCEPTION", "safe_error_message": str(exc)[:500], "retryable": True}], "performance": {"total_ms": timer.ms()}}, completed=True, total_ms=timer.ms())
            raise
        finally:
            if token is not None:
                _CURRENT_TRACE.reset(token)

    traced_hosted._shvya_ai_trace_wrapped = True
    execution.execute_hosted_ai_engagement = traced_hosted


def install_ai_trace_runtime():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _wrap_context_rag()
    _wrap_engagement_service()
    _wrap_crm_executor()
    _wrap_api_executor()
    _wrap_hosted_executor()
