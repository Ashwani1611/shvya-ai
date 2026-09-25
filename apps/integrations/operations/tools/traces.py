"""Secure, tenant-scoped production AI trace inspection."""

from copy import deepcopy

from apps.ai_engagement.models import AITrace
from apps.integrations.diagnostic_auth import sanitize_data, sanitize_text
from apps.integrations.operations_tools import (
    OperationsToolError,
    ToolExecution,
    _organization_for,
    _require_operations_capability,
    _uuid,
)
from apps.integrations.operations_policy import CAP_DIAGNOSTICS_READ, CAP_TRACE_CONTENT_READ


def _trace_content(details):
    details = details if isinstance(details, dict) else {}
    generation = details.get("generation") if isinstance(details.get("generation"), dict) else {}
    input_data = details.get("input") if isinstance(details.get("input"), dict) else {}
    attribution = details.get("attribution") if isinstance(details.get("attribution"), dict) else {}
    return {
        "rendered_prompt": generation.get("rendered_prompt") or generation.get("prompt") or details.get("rendered_prompt"),
        "model_input": generation.get("model_input") or input_data.get("model_input"),
        "model_output": generation.get("model_output") or generation.get("generated_response"),
        "message_attribution": attribution or details.get("message_attribution"),
        "run_references": details.get("run_references") or details.get("runs") or {},
    }


def get_production_trace(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_DIAGNOSTICS_READ)
    trace_id = _uuid((arguments or {}).get("trace_id"), field="trace_id")
    trace = AITrace.objects.filter(pk=trace_id, organization=organization).select_related("lead").first()
    if trace is None:
        raise OperationsToolError("AI trace not found in this organization.")
    include_content = bool((arguments or {}).get("include_content", False))
    if include_content:
        _require_operations_capability(identity=identity, organization=organization, capability=CAP_TRACE_CONTENT_READ)
    details = deepcopy(trace.details or {}) if include_content else {}
    content = _trace_content(details) if include_content else None
    response = {
        "trace": {
            "id": str(trace.id),
            "organization_id": str(trace.organization_id),
            "lead_id": str(trace.lead_id),
            "source_inbound_message_id": str(trace.source_inbound_message_id),
            "outbound_message_id": str(trace.outbound_message_id) if trace.outbound_message_id else None,
            "pipeline_id": str(trace.pipeline_id) if trace.pipeline_id else None,
            "stage_id": str(trace.stage_id) if trace.stage_id else None,
            "whatsapp_account_id": str(trace.whatsapp_account_id) if trace.whatsapp_account_id else None,
            "status": trace.status,
            "reason_code": trace.reason_code,
            "execution_path": trace.execution_path,
            "model_name": trace.model_name,
            "total_ms": trace.total_ms,
            "started_at": trace.started_at.isoformat(),
            "completed_at": trace.completed_at.isoformat() if trace.completed_at else None,
        },
        "content_access": "granted" if include_content else "metadata_only",
        "available_sections": sorted((trace.details or {}).keys()) if isinstance(trace.details, dict) else [],
        "credentials_returned": False,
    }
    if include_content:
        response["content"] = sanitize_data(content, text_limit=12000, list_limit=100)
        response["response_sanitized"] = True
    else:
        response["previews"] = {
            "incoming_message": sanitize_text(trace.incoming_message_preview, limit=240),
            "response_message": sanitize_text(trace.response_preview, limit=240),
        }
    return ToolExecution(data=response, capability=CAP_TRACE_CONTENT_READ if include_content else "diagnostics.read", target_type="ai_trace", target_id=str(trace.id), audit_summary={"content_access": "granted" if include_content else "metadata_only"})
