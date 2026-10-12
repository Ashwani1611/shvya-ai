"""Link publish and worker attempts without storing task arguments/results."""

from contextvars import ContextVar
from datetime import timedelta
import logging
import uuid

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.core.observability import REQUEST_ID, TASK_ID

from .services import (
    activity_context, annotate_activity, finish_activity, get_context,
    record_event, start_activity,
)


logger = logging.getLogger("shvya.operations")
_EXECUTIONS = ContextVar("shvya_api_task_executions", default=())


def _lookup_failed(action, exc):
    try:
        logger.warning("api_activity.%s exception=%s", action, type(exc).__name__)
    except Exception:
        pass  # A broken logging handler must not affect worker execution.


def task_integration(name):
    for fragment, integration in (
        ("instagram", "instagram"), ("hosted", "whatsapp_hosted"),
        ("whatsapp", "whatsapp_api"), ("campaign", "whatsapp_api"),
        ("indiamart", "indiamart"), ("justdial", "justdial"),
        ("meta_lead", "meta_leads"), ("acres99", "99acres"),
        ("ai.", "ai"), ("ai_engagement", "ai"),
    ):
        if fragment in str(name).lower():
            return integration
    return "shvya_api"


def publish_activity(name, headers, queue):
    """A queued row records intent, not a guarantee of broker persistence."""
    if not isinstance(headers, dict) or not headers.get("id"):
        return
    headers["shvya_request_id"] = REQUEST_ID.get()
    if not getattr(settings, "API_ACTIVITY_ENABLED", True):
        return
    inherited = get_context()
    headers["shvya_activity_context"] = {
        key: str(value) for key, value in inherited.items()
        if key in {"activity_id", "correlation_id", "organization_id", "organization_reference", "connection_id"}
        and value
    }
    due = timezone.now()
    try:
        eta = parse_datetime(str(headers.get("eta") or ""))
        if eta:
            if timezone.is_naive(eta):
                eta = timezone.make_aware(eta)
            due = max(due, eta)
    except (TypeError, ValueError, OverflowError):
        pass
    activity = start_activity(
        integration=task_integration(name), operation=str(name), kind="task",
        direction="internal", status="queued", task_id=str(headers["id"]),
        request_id=REQUEST_ID.get(),
        expected_by=due + timedelta(seconds=max(
            1, int(getattr(settings, "API_ACTIVITY_QUEUE_DEADLINE_SECONDS", 300)),
        )),
        next_step="Worker start",
        metadata={"queue": queue, "attempt": int(headers.get("retries") or 0) + 1,
                  "scheduled_for": due.isoformat()},
        summary="Task publish requested; awaiting worker execution.",
    )
    if activity:
        headers["shvya_activity_id"] = str(activity.id)


def _published_activity(headers):
    if not getattr(settings, "API_ACTIVITY_ENABLED", True):
        return None
    try:
        activity_id = uuid.UUID(str(headers.get("shvya_activity_id") or ""))
    except (ValueError, TypeError, AttributeError):
        return None
    try:
        with transaction.atomic():
            return apps.get_model("core", "APIActivity").objects.filter(pk=activity_id).first()
    except Exception as exc:
        # Never allow observability to break task execution or an outer transaction.
        _lookup_failed("task_lookup_failed", exc)
        return None


def _claim_published_activity(headers):
    """Claim one queued attempt under a row lock; redeliveries get new rows."""
    if not getattr(settings, "API_ACTIVITY_ENABLED", True):
        return None, False
    try:
        activity_id = uuid.UUID(str(headers.get("shvya_activity_id") or ""))
    except (ValueError, TypeError, AttributeError):
        return None, False
    try:
        with transaction.atomic():
            activity = apps.get_model("core", "APIActivity").objects.select_for_update().filter(pk=activity_id).first()
            if activity is None or activity.status != "queued":
                return activity, False
            claimed = annotate_activity(
                activity, status="processing", expected_by=_execution_deadline(),
                next_step="Worker completion",
            )
            return (activity, True) if claimed is not None else (activity, False)
    except Exception as exc:
        _lookup_failed("task_claim_failed", exc)
        return None, False


def _execution_deadline():
    return timezone.now() + timedelta(seconds=max(
        1, int(getattr(settings, "API_ACTIVITY_TASK_DEADLINE_SECONDS", 900)),
    ))


def confirm_publish(headers):
    activity = _published_activity(headers or {})
    if activity:
        record_event(activity, name="task.published", status="queued",
                     summary="Broker publish returned; awaiting worker execution.")


def start_execution(task_id, name, headers):
    headers = headers if isinstance(headers, dict) else {}
    request_token = REQUEST_ID.set(str(headers.get("shvya_request_id") or REQUEST_ID.get() or ""))
    task_token = TASK_ID.set(str(task_id or ""))
    activity, claimed = _claim_published_activity(headers)
    # A redelivered task gets a separate attempt; never erase the original.
    if activity is not None and not claimed:
        inherited = {"activity_id": str(activity.id), "correlation_id": str(activity.correlation_id),
                     "organization_id": str(activity.organization_id or "")}
        activity = None
    else:
        inherited = headers.get("shvya_activity_context") or get_context()
    with activity_context(inherited):
        if activity is None:
            activity = start_activity(
                integration=task_integration(name), operation=name,
                kind="task", direction="internal", task_id=str(task_id or ""),
                request_id=REQUEST_ID.get(), metadata={"queue": headers.get("shvya_queue", "celery")},
            )
    if activity:
        if not claimed:
            annotate_activity(
                activity, status="processing", expected_by=_execution_deadline(),
                next_step="Worker completion",
            )
        record_event(activity, name="task.started", status="processing",
                     summary="Worker began this execution attempt.")
    manager = activity_context(activity)
    manager.__enter__()
    _EXECUTIONS.set((*_EXECUTIONS.get(), (str(task_id), activity, manager, request_token, task_token)))


def current_activity(task_id=None):
    stack = _EXECUTIONS.get()
    if not stack or (task_id is not None and stack[-1][0] != str(task_id)):
        return None
    return stack[-1][1]


def fail_execution(task_id, exception):
    finish_activity(
        current_activity(task_id), status="failed", error_category="worker",
        error_code=type(exception).__name__ if exception else "TaskFailure",
        summary="Worker execution failed; inspect related activity for provider outcomes.",
    )


def retry_execution(task_id, reason):
    finish_activity(
        current_activity(task_id), status="failed", error_category="retry",
        error_code=type(reason).__name__ if reason else "Retry",
        summary="This attempt requested a retry; the next execution is tracked separately.",
        metadata={"retry_scheduled": True},
    )


def finish_execution(task_id, state, duration_ms=None):
    stack = _EXECUTIONS.get()
    if not stack or stack[-1][0] != str(task_id):
        return
    _, activity, manager, request_token, task_token = stack[-1]
    try:
        if state not in {"FAILURE", "RETRY"}:
            status = {"SUCCESS": "completed", "REVOKED": "blocked"}.get(state, "unknown")
            finish_activity(
                activity, status=status, duration_ms=duration_ms,
                summary=("Worker execution completed; business outcomes are recorded separately."
                         if status == "completed" else "Worker ended without a confirmed successful execution."),
            )
        elif activity and duration_ms is not None:
            annotate_activity(activity, duration_ms=duration_ms)
    finally:
        manager.__exit__(None, None, None)
        REQUEST_ID.reset(request_token)
        TASK_ID.reset(task_token)
        _EXECUTIONS.set(stack[:-1])
