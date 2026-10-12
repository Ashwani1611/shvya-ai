"""Best-effort activity capture with bounded metadata and scoped correlation.

Each database write uses its own savepoint. Capture failures never mask a
business result, and emit a content-free failure signal through the existing
operations logger/cache. An outer business transaction can still roll back its
activity records: this ledger is diagnostic evidence, not a transactional outbox.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import logging
import uuid

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import models, transaction
from django.utils import timezone

from .sanitization import sanitize_endpoint, sanitize_identifier, sanitize_metadata, sanitize_text

logger = logging.getLogger("shvya.operations")
_CONTEXT = ContextVar("api_activity_context", default=None)
_FAILURES_KEY = "shvya:api_activity:failed_writes:v1"
_LAST_FAILURE_KEY = "shvya:api_activity:last_failure:v1"
_HEALTH_TTL = 8 * 24 * 60 * 60
_TERMINAL = frozenset({"completed", "failed", "unknown", "blocked", "skipped", "partial"})
_CONTEXT_FIELDS = ("activity_id", "correlation_id", "organization_id", "organization_reference", "connection_id")


def _enabled():
    return bool(getattr(settings, "API_ACTIVITY_ENABLED", True))


def _failed(action, exc):
    # Never include exception messages, query strings, SQL or tracebacks here.
    try:
        logger.warning(json.dumps({
            "event": "api_activity.capture_failed", "action": action,
            "error_type": type(exc).__name__,
        }, separators=(",", ":")))
    except Exception:
        pass  # A broken logging handler must not break the business operation.
    try:
        cache.add(_FAILURES_KEY, 0, timeout=_HEALTH_TTL)
        cache.incr(_FAILURES_KEY)
        cache.set(_LAST_FAILURE_KEY, timezone.now().isoformat(), timeout=_HEALTH_TTL)
    except Exception:
        pass  # The independent safe log above remains the fallback signal.


def capture_health():
    """Read capture-failure signals; missing cache is not reported as healthy."""
    try:
        values = cache.get_many((_FAILURES_KEY, _LAST_FAILURE_KEY))
        return {
            "available": True,
            "failed_writes": int(values.get(_FAILURES_KEY, 0)),
            "last_failure_at": values.get(_LAST_FAILURE_KEY),
            "enabled": _enabled(),
        }
    except Exception:
        return {
            "available": False, "failed_writes": None,
            "last_failure_at": None, "enabled": _enabled(),
        }


def _uuid(value):
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def get_context():
    """Return a copy so downstream code cannot mutate the current context."""
    return dict(_CONTEXT.get() or {})


def bind_organization(organization_id):
    """Bind only a server-authenticated tenant inside an existing activity scope.

    The caller must authenticate/resolve the organization first. This helper
    never accepts a request's claimed tenant as authority. The enclosing
    activity_context token restores the original context after the request/task.
    """
    context = get_context()
    if not context.get("activity_id"):
        return None
    try:
        activity_id = _uuid(context["activity_id"])
        organization_id = _uuid(organization_id)
    except (ValueError, TypeError, AttributeError):
        return None
    if activity_id is None or organization_id is None:
        return None
    result = annotate_activity(activity_id, organization_id=organization_id)
    # Preserve authenticated context even if diagnostic persistence is down.
    context["organization_id"] = str(organization_id)
    context["organization_reference"] = str(organization_id)
    _CONTEXT.set(context)
    return result


@contextmanager
def activity_context(activity):
    """Bind a model or validated internal context, resetting even on errors."""
    if activity is None:
        yield get_context()
        return
    if isinstance(activity, dict):
        values = {key: activity[key] for key in _CONTEXT_FIELDS if key in activity}
    else:
        values = {
            "activity_id": getattr(activity, "pk", None),
            "correlation_id": getattr(activity, "correlation_id", None),
            "organization_id": getattr(activity, "organization_id", None),
            "organization_reference": getattr(activity, "organization_reference", ""),
            "connection_id": getattr(activity, "connection_id", ""),
        }
    context = {}
    for key in ("activity_id", "correlation_id", "organization_id"):
        try:
            parsed = _uuid(values.get(key))
        except (TypeError, ValueError, AttributeError):
            parsed = None
        if parsed:
            context[key] = str(parsed)
    if context.get("organization_id"):
        context["organization_reference"] = context["organization_id"]
    if values.get("connection_id"):
        context["connection_id"] = sanitize_identifier(values["connection_id"], max_length=80)
    token = _CONTEXT.set(context)
    try:
        yield dict(context)
    finally:
        _CONTEXT.reset(token)


def _clean_fields(model, supplied, *, creating=False):
    supplied = dict(supplied)
    if "organization" in supplied:
        organization = supplied.pop("organization")
        supplied["organization_id"] = getattr(organization, "pk", organization)
    output = {}
    for field in model._meta.concrete_fields:
        name = field.attname
        if name not in supplied or name in {"created_at", "updated_at"}:
            continue
        if name == "id" and not creating:
            continue
        value = supplied[name]
        if isinstance(field, models.ForeignKey) or isinstance(field, models.UUIDField):
            output[name] = _uuid(value)
        elif isinstance(field, models.JSONField):
            output[name] = sanitize_metadata(value)
        elif name == "status":
            if value not in model.Status.values:
                raise ValueError("Invalid activity status")
            output[name] = value
        elif name == "endpoint":
            output[name] = sanitize_endpoint(value)
        elif isinstance(field, models.CharField):
            cleaner = sanitize_identifier if name in {
                "connection_id", "entity_id", "request_id", "task_id", "provider_reference",
                "organization_reference",
            } else sanitize_text
            output[name] = cleaner(value, max_length=field.max_length)
        elif isinstance(field, models.DateTimeField):
            if value is not None and not isinstance(value, datetime):
                raise ValueError("Activity timestamp must be a datetime")
            output[name] = value
        elif name == "http_status":
            output[name] = None if value is None else max(100, min(int(value), 599))
        elif name == "duration_ms":
            output[name] = None if value is None else max(0, min(int(value), 2**63 - 1))
    if "organization_id" in output:
        output["organization_reference"] = str(output["organization_id"] or "")
    return output


def _event_values(*, name, status="", summary="", metadata=None):
    model = apps.get_model("core", "APIActivity")
    if status and status not in model.Status.values:
        raise ValueError("Invalid activity event status")
    return {
        "name": sanitize_text(name, max_length=80),
        "status": status,
        "summary": sanitize_text(summary, max_length=1000),
        "metadata": sanitize_metadata(metadata),
    }


def _create(*, event_name, fields):
    if not _enabled():
        return None
    try:
        model = apps.get_model("core", "APIActivity")
        event_model = apps.get_model("core", "APIActivityEvent")
        context = get_context()
        fields = dict(fields)
        if "correlation_id" not in fields and context.get("correlation_id"):
            fields["correlation_id"] = context["correlation_id"]
        if "parent_id" not in fields and context.get("activity_id"):
            fields["parent_id"] = context["activity_id"]
        if "organization_id" not in fields and "organization" not in fields and context.get("organization_id"):
            fields["organization_id"] = context["organization_id"]
        if "connection_id" not in fields and context.get("connection_id"):
            fields["connection_id"] = context["connection_id"]
        cleaned = _clean_fields(model, fields, creating=True)
        # Explicit null correlation/id should still have a usable stable UUID.
        for key in ("id", "correlation_id"):
            if cleaned.get(key) is None:
                cleaned.pop(key, None)
        if cleaned.get("status") in _TERMINAL and not cleaned.get("finished_at"):
            cleaned["finished_at"] = timezone.now()
        with transaction.atomic():
            activity = model.objects.create(**cleaned)
            event_model.objects.create(activity=activity, **_event_values(
                name=event_name, status=activity.status, summary=activity.summary,
                metadata=activity.metadata,
            ))
        return activity
    except Exception as exc:
        _failed("start", exc)
        return None


def start_activity(**fields):
    """Create an activity; only server-resolved tenant context may be supplied."""
    return _create(event_name="started", fields=fields)


def record_activity(**fields):
    """Record a completed business observation without a second database write."""
    fields.setdefault("status", "completed")
    return _create(event_name="recorded", fields=fields)


def _persist_update(activity, changes, *, event_name, event_metadata=None):
    model = apps.get_model("core", "APIActivity")
    event_model = apps.get_model("core", "APIActivityEvent")
    activity_id = _uuid(getattr(activity, "pk", activity))
    cleaned = _clean_fields(model, changes)
    with transaction.atomic():
        # Serialize updates so concurrent completions do not erase each other.
        current = model.objects.select_for_update().get(pk=activity_id)
        if "metadata" in cleaned and isinstance(current.metadata, dict) and isinstance(cleaned["metadata"], dict):
            cleaned["metadata"] = sanitize_metadata({**current.metadata, **cleaned["metadata"]})
        for name, value in cleaned.items():
            setattr(current, name, value)
        current.save(update_fields=[*cleaned, "updated_at"])
        event_model.objects.create(activity=current, **_event_values(
            name=event_name, status=current.status, summary=changes.get("summary", ""),
            metadata=event_metadata,
        ))
    # Keep the original object useful for callers that don't reassign the result.
    if isinstance(activity, model):
        for name in (*cleaned, "updated_at"):
            setattr(activity, name, getattr(current, name))
    return current


def annotate_activity(activity, **fields):
    """Attach late-authenticated tenant/entity evidence or an explicit state."""
    if activity is None or not _enabled():
        return None
    try:
        for key in ("id", "finished_at", "created_at", "updated_at"):
            fields.pop(key, None)
        if "status" in fields:
            fields["finished_at"] = timezone.now() if fields["status"] in _TERMINAL else None
        return _persist_update(activity, fields, event_name="annotated", event_metadata={
            "changed_fields": sorted(fields)[:40],
        })
    except Exception as exc:
        _failed("annotate", exc)
        return None


def finish_activity(
    activity, *, status, duration_ms=None, http_status=None, error_code=None,
    error_category=None, error_message=None, summary=None,
    provider_reference=None, metadata=None,
):
    """Persist a state transition; omitted optional fields retain prior evidence."""
    if activity is None or not _enabled():
        return None
    try:
        values = {
            "duration_ms": duration_ms, "http_status": http_status,
            "error_code": error_code, "error_category": error_category,
            "error_message": error_message, "summary": summary,
            "provider_reference": provider_reference, "metadata": metadata,
        }
        changes = {key: value for key, value in values.items() if value is not None}
        changes["status"] = status
        changes["finished_at"] = timezone.now() if status in _TERMINAL else None
        if status in _TERMINAL:
            changes["expected_by"] = None
            changes["next_step"] = ""
        return _persist_update(activity, changes, event_name=status, event_metadata=metadata)
    except Exception as exc:
        _failed("finish", exc)
        return None


def record_event(activity, *, name, status="", summary="", metadata=None):
    """Append evidence without implicitly mutating the activity's current state."""
    if activity is None or not _enabled():
        return None
    try:
        event_model = apps.get_model("core", "APIActivityEvent")
        activity_id = _uuid(getattr(activity, "pk", activity))
        with transaction.atomic():
            return event_model.objects.create(activity_id=activity_id, **_event_values(
                name=name, status=status, summary=summary, metadata=metadata,
            ))
    except Exception as exc:
        _failed("event", exc)
        return None
