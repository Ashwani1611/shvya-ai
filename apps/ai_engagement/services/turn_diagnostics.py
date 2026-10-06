"""Bounded diagnostic codes, never prompts, customer data or provider error text."""
from functools import wraps
import re


def identifier(value):
    value = str(value or "")
    return value if re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,120}", value) else ""


def provider_diagnostics(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        from apps.ai_engagement.services import trace_service
        if provider_quota_exhausted():
            from apps.ai_engagement.services.ai_provider import AIProviderQuotaError
            raise AIProviderQuotaError("OpenAI API billing quota is exhausted.")
        metadata = kwargs.get("metadata") or {}
        entry = {"phase": identifier(metadata.get("phase")) or "generation"}
        try:
            result = method(self, *args, **kwargs)
            entry.update(status="ok", model=identifier(result.model))
            return result
        except Exception as exc:
            entry.update(status="failed", error_type=type(exc).__name__)
            from apps.ai_engagement.services.ai_provider import AIProviderQuotaError
            if isinstance(exc, AIProviderQuotaError):
                trace_service.record("provider", billing_exhausted=True)
            cause = exc.__cause__ or exc
            body = getattr(cause, "body", None)
            if isinstance(body, dict):
                detail = body.get("error", body)
                if isinstance(detail, dict):
                    for key in ("code", "type", "param"):
                        entry[key] = identifier(detail.get(key))
            status = getattr(cause, "status_code", None)
            if isinstance(status, int):
                entry["http_status"] = status
            raise
        finally:
            trace_service.append("provider", "calls", entry)
    return wrapped


def record_failure(exc):
    from apps.ai_engagement.services import trace_service
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)
    frame = frames[-1] if frames else None
    # A terminal standard-library frame cannot identify a recursive application
    # call loop. Keep only bounded code locations, never source lines or locals.
    app_sites = [
        item.filename.rsplit("/", 1)[-1] + ":" + str(item.lineno)
        for item in frames if "/apps/ai_engagement/" in item.filename
    ]
    repeated_sites = sorted(set(app_sites), key=app_sites.count, reverse=True)[:6]
    trace_service.record("sandbox_failure", {
        "error_type": type(exc).__name__,
        "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else "",
        "site": (frame.filename.rsplit("/", 1)[-1] + ":" + str(frame.lineno)) if frame else "",
        "application_sites": repeated_sites,
    })


def provider_quota_exhausted():
    from apps.ai_engagement.services import trace_service
    trace = trace_service.current()
    return bool(trace and (trace.data.get("provider") or {}).get("billing_exhausted"))


def _count(value):
    return min(max(value, 0), 30) if type(value) is int else 0


def _decision_details(trace, result):
    """Useful successful Sandbox counters without prompts, IDs or provider prose."""
    parts = []
    knowledge = trace.data.get("knowledge_retrieval")
    if isinstance(knowledge, dict):
        parts.append("knowledge/chunks=" + str(_count(knowledge.get("retained_count")))
                     + "/websites=" + str(_count(knowledge.get("website_count")))
                     + "/uploads=" + str(_count(knowledge.get("uploaded_count"))))
    files = trace.data.get("file_decision") or {}
    files = files if isinstance(files, dict) else {}
    phases = [(phase, files.get(phase) if isinstance(files.get(phase), dict) else {}) for phase in ("draft", "final")]
    relevant = any(item.get("explicit_request") is True or item.get("welcome_due") is True or _count(item.get("candidate_count"))
                   or _count(item.get("selected_count")) for _, item in phases)
    if result is not None and not files:
        from apps.ai_engagement.services.file_sharing import explicit_file_request
        if explicit_file_request(str(getattr(result, "message", "") or "")):
            relevant = True
            parts.append("file/draft/path=not_observed")
    statuses = {"final_language_only", "already_selected", "silenced", "not_requested",
                "no_candidates", "pending", "selected", "declined", "failed"}
    if relevant:
        for phase, item in phases:
            if not item:
                continue
            status = item.get("review_status")
            status = status if isinstance(status, str) and status in statuses else "unknown"
            fields = {"candidates": _count(item.get("candidate_count")), "review": status,
                      "selected": _count(item.get("selected_count")),
                      "validated": _count(item.get("validated_selected_count")),
                      "validation_drop": _count(item.get("validation_drop")),
                      "graph": _count(item.get("graph_selected_count"))}
            if item.get("review_trigger") in ("explicit_request", "welcome", "none"):
                fields["trigger"] = item["review_trigger"]
            if isinstance(item.get("grounding_status"), str) and item["grounding_status"] in {"approved", "rejected"}:
                fields["grounding"] = item["grounding_status"]
            parts.append("file/" + phase + "/" + "/".join(f"{key}={value}" for key, value in fields.items()))
        if result is not None:
            parts.append("file/preview=" + str(min(len(getattr(result, "files", []) or []), 30)))
    capture = trace.data.get("qualification_capture") or {}
    capture = capture if isinstance(capture, dict) else {}
    capture_relevant = bool(capture) and (
        capture.get("review_status") not in ("skipped", "no_candidates")
        or _count(capture.get("candidate_count")) or _count(capture.get("accepted_count"))
    )
    if capture_relevant:
        status = capture.get("review_status")
        status = status if isinstance(status, str) and status in {"skipped", "no_candidates", "reviewed", "rejected", "failed"} else "unknown"
        parts.append("capture/candidates=" + str(_count(capture.get("candidate_count")))
                     + "/review=" + status + "/accepted=" + str(_count(capture.get("accepted_count"))))
    if relevant or capture_relevant:
        draft = dict(phases)["draft"]
        parts.append("capture/proposed=" + str(_count(draft.get("proposed_capture_count")))
                     + "/graph=" + str(_count(draft.get("graph_capture_count"))))
        counts = {}
        allowed_phases = {"primary", "intent_classification", "file_selection_review", "qualification_capture_recovery",
                          "grounding", "grounding_contract_retry", "grounding_reply_repair", "schema_repair",
                          "final_reply_language", "final_reply_language_validation"}
        for call in (trace.data.get("provider") or {}).get("calls", []):
            phase, status = call.get("phase"), call.get("status")
            if isinstance(phase, str) and isinstance(status, str) and phase in allowed_phases and status in {"ok", "failed"}:
                key = phase + ":" + status
                counts[key] = min(counts.get(key, 0) + 1, 30)
        if counts:
            parts.append("calls/" + "/".join(f"{key}={value}" for key, value in counts.items()))
    return parts


def summary(result=None):
    from apps.ai_engagement.services import trace_service
    trace = trace_service.current()
    if trace is None:
        return ""
    parts = []
    recovery = trace.data.get("recovery") or {}
    transient_recovered = bool(
        isinstance(recovery, dict)
        and recovery.get("provider_transient_recovered") is True
    )
    for call in (trace.data.get("provider") or {}).get("calls", []):
        if call.get("status") == "failed":
            if transient_recovered and call.get("error_type") == "AIProviderTransientError":
                continue
            parts.append("/".join(str(call[key]) for key in
                ("phase", "error_type", "http_status", "code", "type", "param")
                if call.get(key)))
    if transient_recovered:
        parts.append("recovery/provider_rate_limit_recovered")
    grounding = trace.data.get("grounding") or {}
    if grounding.get("approved") is False:
        parts.append("validation/" + identifier(grounding.get("validation_reason")))
    error = trace.data.get("error") or {}
    if error.get("error_type") and not (
        transient_recovered
        and error.get("error_type") in {"AIProviderTransientError", "EngagementError"}
    ):
        parts.append("runtime/" + identifier(error.get("error_type")))
    failure = trace.data.get("sandbox_failure") or {}
    if failure:
        parts.append("runtime/" + "/".join(str(failure.get(key) or "") for key in ("error_type", "cause_type", "site")))
        if failure.get("application_sites"):
            parts.append("application/" + "/".join(failure["application_sites"]))
    parts.extend(_decision_details(trace, result))
    return "; ".join(dict.fromkeys(parts))[:1000]


def sandbox_diagnostics(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        from dataclasses import replace
        from apps.ai_engagement.services import trace_service
        from apps.ai_engagement.services.playground import PlaygroundError
        token = trace_service.begin_sandbox_trace(
            organization=kwargs.get("organization"), message=kwargs.get("message", ""),
        )
        try:
            result = method(self, *args, **kwargs)
            trace_service.record("generation", {
                "generated_response": result.response, "model": result.model,
                "should_engage": result.should_engage,
            })
            trace_service.record("finalization", {
                "execution_mode": "sandbox_preview", "live_actions_executed": False,
                "preview_events": result.events, "preview_files": result.files,
            })
            trace_service.record("brain_bundle", result.brain_bundle)
            buffer = trace_service.current()
            if buffer is not None:
                buffer.data["status"] = "completed" if result.should_engage else "silenced"
            return replace(result, diagnostics=summary(result), trace_id=buffer.trace_id if buffer else None)
        except PlaygroundError as exc:
            trace_service.mark_error(step="sandbox", exc=exc, code="SANDBOX_PREVIEW_FAILED")
            buffer = trace_service.current()
            if buffer is not None:
                buffer.data["status"] = "failed"
            raise
        except Exception as exc:
            trace_service.mark_error(step="sandbox", exc=exc, code="SANDBOX_PREVIEW_EXCEPTION")
            buffer = trace_service.current()
            if buffer is not None:
                buffer.data["status"] = "failed"
            raise
        finally:
            trace_service.flush(reset_token=token)
    return wrapped
