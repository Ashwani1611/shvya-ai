"""Attach existing application diagnostics to the current activity timeline."""

from .services import get_context, record_event


_FIELDS = frozenset({
    "organization_id", "lead_id", "message_id", "account_id", "job_id",
    "model", "feature", "provider", "queue", "task", "exception",
    "attempt", "retry_count", "duration_ms", "latency_ms", "status_code",
    "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
    "total_tokens", "message_count", "token_count", "prompt_version",
})


def observe_operational_event(event, *, level, fields):
    context = get_context()
    if not context.get("activity_id"):
        return
    record_event(
        context["activity_id"], name=event,
        metadata={"level": level, **{key: value for key, value in fields.items() if key in _FIELDS}},
    )
