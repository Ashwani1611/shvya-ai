"""Celery signals for queue latency, execution and retry observability."""

from __future__ import annotations

import logging
import time

from celery.signals import (
    after_task_publish,
    before_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
)
from django.conf import settings
import redis

from apps.core.observability import (
    bind_context,
    emit_event,
    increment,
    observe_latency,
)
from apps.core.api_activity.celery import (
    confirm_publish, fail_execution, finish_execution, publish_activity,
    retry_execution, start_execution,
)


_STARTED = {}


def _broker():
    return redis.Redis.from_url(settings.CELERY_BROKER_URL)


def _queue_name(sender=None, routing_key=None):
    routes = getattr(settings, "CELERY_TASK_ROUTES", {}) or {}
    route = routes.get(str(sender), {}) if isinstance(routes, dict) else {}
    return str(routing_key or route.get("queue") or "celery")


@before_task_publish.connect
def task_published(sender=None, headers=None, routing_key=None, **kwargs):
    headers = headers if isinstance(headers, dict) else {}
    task_id = str(headers.get("id") or "")
    queue = _queue_name(sender, routing_key)
    published_at = time.time()
    headers["shvya_enqueued_at"] = published_at
    headers["shvya_queue"] = queue
    if not task_id:
        return
    publish_activity(str(sender or "unknown"), headers, queue)
    try:
        client = _broker()
        client.zadd(f"shvya:queue-age:{queue}", {task_id: published_at})
        client.expire(f"shvya:queue-age:{queue}", 8 * 24 * 60 * 60)
    except Exception:
        emit_event("celery.queue_metric.failed", level=logging.WARNING, queue=queue)


@after_task_publish.connect
def task_publish_confirmed(headers=None, **kwargs):
    confirm_publish(headers)


@task_prerun.connect
def task_started(sender=None, task_id=None, task=None, **kwargs):
    request = getattr(task, "request", None)
    headers = getattr(request, "headers", {}) or {}
    task_name = str(getattr(sender, "name", "") or getattr(task, "name", "unknown"))
    queue = str(headers.get("shvya_queue") or "celery")
    enqueued_at = headers.get("shvya_enqueued_at")
    start_execution(task_id, task_name, headers)
    bind_context(task_id=str(task_id or ""))
    _STARTED[str(task_id)] = time.perf_counter()
    if enqueued_at:
        observe_latency(
            "celery.queue_latency_ms",
            max(0, (time.time() - float(enqueued_at)) * 1000),
            labels={"queue": queue, "task": task_name},
        )
    try:
        _broker().zrem(f"shvya:queue-age:{queue}", str(task_id))
    except Exception:
        pass
    increment("celery.tasks.started", labels={"queue": queue, "task": task_name})


@task_postrun.connect
def task_finished(sender=None, task_id=None, task=None, state=None, **kwargs):
    task_name = str(getattr(sender, "name", "") or getattr(task, "name", "unknown"))
    request = getattr(task, "request", None)
    headers = getattr(request, "headers", {}) or {}
    queue = str(headers.get("shvya_queue") or "celery")
    started = _STARTED.pop(str(task_id), None)
    if started is not None:
        observe_latency(
            "celery.task_duration_ms",
            (time.perf_counter() - started) * 1000,
            labels={"queue": queue, "task": task_name, "state": state or "unknown"},
        )
    increment(
        "celery.tasks.completed",
        labels={"queue": queue, "task": task_name, "state": state or "unknown"},
    )
    finish_execution(
        task_id, state,
        duration_ms=((time.perf_counter() - started) * 1000 if started is not None else None),
    )


@task_failure.connect
def task_failed(sender=None, task_id=None, exception=None, **kwargs):
    fail_execution(task_id, exception)
    task_name = str(getattr(sender, "name", "unknown"))
    increment("celery.tasks.failed", labels={"task": task_name})
    emit_event(
        "celery.task.failed",
        level=logging.ERROR,
        task=task_name,
        exception=type(exception).__name__ if exception else "unknown",
    )


@task_retry.connect
def task_retried(sender=None, request=None, reason=None, **kwargs):
    retry_execution(getattr(request, "id", None), reason)
    task_name = str(getattr(sender, "name", "unknown"))
    increment("celery.tasks.retried", labels={"task": task_name})
    emit_event(
        "celery.task.retried",
        level=logging.WARNING,
        task=task_name,
        reason=type(reason).__name__ if reason else "retry",
    )
