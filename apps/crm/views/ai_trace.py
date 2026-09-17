from __future__ import annotations

from datetime import datetime, time

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from apps.ai_engagement.models import AITrace
from apps.crm.authentication import crm_login_required
from apps.crm.models import Pipeline


def _org_trace(organization, trace_id):
    trace = (
        AITrace.objects.filter(pk=trace_id, organization=organization)
        .select_related("lead")
        .first()
    )
    if trace is None:
        raise Http404("AI trace not found")
    return trace


def _trace_detail_context(details):
    """Expose bounded Phase 1-7 trace sections to the server-rendered detail UI."""
    details = details if isinstance(details, dict) else {}
    return {
        "input": details.get("input", {}),
        "permission": details.get("permission", {}),
        "intent": details.get("intent", {"status": "NOT_AVAILABLE"}),
        "qualification": details.get("qualification", {}),
        "policy": details.get("policy", {}),
        "rag": details.get("rag", {}),
        "grounding": details.get("grounding", {}),
        "memory": details.get("memory", {}),
        "generation": details.get("generation", {}),
        "action_plan": details.get("action_plan", {}),
        "crm_actions": details.get("crm_actions", {}),
        "finalization": details.get("finalization", {}),
        "performance": details.get("performance", {}),
        "provider_usage": details.get("provider_usage", {}),
        "error": details.get("error", {}),
        "delivery": details.get("delivery", {}),
    }


@crm_login_required
def ai_trace_list_view(request):
    organization = request.crm_user.organization
    traces = AITrace.objects.filter(organization=organization).select_related("lead")

    status = request.GET.get("status", "").strip()
    connection_type = request.GET.get("connection_type", "").strip()
    pipeline_id = request.GET.get("pipeline", "").strip()
    search = request.GET.get("q", "").strip()
    date_raw = request.GET.get("date", "").strip()

    if status:
        traces = traces.filter(status=status)
    if connection_type:
        traces = traces.filter(connection_type=connection_type)
    if pipeline_id:
        traces = traces.filter(pipeline_id=pipeline_id)
    if search:
        traces = traces.filter(lead__name__icontains=search)
    if date_raw:
        try:
            day = datetime.strptime(date_raw, "%Y-%m-%d").date()
            start = timezone.make_aware(datetime.combine(day, time.min))
            end = timezone.make_aware(datetime.combine(day, time.max))
            traces = traces.filter(started_at__range=(start, end))
        except ValueError:
            pass

    return render(
        request,
        "crm/knowledge_base/ai_trace_list.html",
        {
            "traces": traces[:250],
            "pipelines": Pipeline.objects.filter(
                organization=organization,
                is_active=True,
            ).order_by("name"),
            "filters": request.GET,
            "status_choices": AITrace.Status.choices,
            "connection_types": ["api", "hosted"],
        },
    )


@crm_login_required
def ai_trace_detail_view(request, trace_id):
    trace = _org_trace(request.crm_user.organization, trace_id)
    details = trace.details if isinstance(trace.details, dict) else {}
    context = {"trace": trace, **_trace_detail_context(details)}
    return render(
        request,
        "crm/knowledge_base/ai_trace_detail.html",
        context,
    )


@crm_login_required
def ai_trace_detail_api(request, trace_id):
    trace = _org_trace(request.crm_user.organization, trace_id)
    details = trace.details if isinstance(trace.details, dict) else {}
    return JsonResponse(
        {
            "id": str(trace.id),
            "status": trace.status,
            "reason_code": trace.reason_code,
            "connection_type": trace.connection_type,
            "execution_path": trace.execution_path,
            "model": trace.model_name,
            "incoming_message": trace.incoming_message_preview,
            "response_message": trace.response_preview,
            "details": details,
            "total_ms": trace.total_ms,
            "started_at": trace.started_at.isoformat(),
            "completed_at": (
                trace.completed_at.isoformat() if trace.completed_at else None
            ),
        }
    )
