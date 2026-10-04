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
    trace_service.record("sandbox_failure", {
        "error_type": type(exc).__name__,
        "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else "",
        "site": (frame.filename.rsplit("/", 1)[-1] + ":" + str(frame.lineno)) if frame else "",
    })


def provider_quota_exhausted():
    from apps.ai_engagement.services import trace_service
    trace = trace_service.current()
    return bool(trace and (trace.data.get("provider") or {}).get("billing_exhausted"))


def summary():
    from apps.ai_engagement.services import trace_service
    trace = trace_service.current()
    if trace is None:
        return ""
    parts = []
    for call in (trace.data.get("provider") or {}).get("calls", []):
        if call.get("status") == "failed":
            parts.append("/".join(str(call[key]) for key in
                ("phase", "error_type", "http_status", "code", "type", "param")
                if call.get(key)))
    grounding = trace.data.get("grounding") or {}
    if grounding.get("approved") is False:
        parts.append("validation/" + identifier(grounding.get("validation_reason")))
    error = trace.data.get("error") or {}
    if error.get("error_type"):
        parts.append("runtime/" + identifier(error.get("error_type")))
    failure = trace.data.get("sandbox_failure") or {}
    if failure:
        parts.append("runtime/" + "/".join(str(failure.get(key) or "") for key in ("error_type", "cause_type", "site")))
    return "; ".join(dict.fromkeys(parts))[:1000]


def sandbox_diagnostics(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        from dataclasses import replace
        from apps.ai_engagement.services import trace_service
        from apps.ai_engagement.services.playground import PlaygroundError
        token = trace_service.begin_trace(organization=kwargs.get("organization"))
        try:
            result = method(self, *args, **kwargs)
            return replace(result, diagnostics=summary())
        except PlaygroundError as exc:
            detail = summary()
            if detail:
                raise PlaygroundError(f"{exc} Diagnostic: {detail}") from exc
            raise
        finally:
            trace_service.flush(reset_token=token)
    return wrapped
