"""Celery signals for queue latency, execution and retry observability."""

from __future__ import annotations

import logging
import time

from celery.signals import (
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
    clear_context,
    emit_event,
    increment,
    observe_latency,
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
    try:
        client = _broker()
        client.zadd(f"shvya:queue-age:{queue}", {task_id: published_at})
        client.expire(f"shvya:queue-age:{queue}", 8 * 24 * 60 * 60)
    except Exception:
        emit_event("celery.queue_metric.failed", level=logging.WARNING, queue=queue)


@task_prerun.connect
def task_started(sender=None, task_id=None, task=None, **kwargs):
    request = getattr(task, "request", None)
    headers = getattr(request, "headers", {}) or {}
    task_name = str(getattr(sender, "name", "") or getattr(task, "name", "unknown"))
    queue = str(headers.get("shvya_queue") or "celery")
    enqueued_at = headers.get("shvya_enqueued_at")
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
    clear_context()


@task_failure.connect
def task_failed(sender=None, task_id=None, exception=None, **kwargs):
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
    task_name = str(getattr(sender, "name", "unknown"))
    increment("celery.tasks.retried", labels={"task": task_name})
    emit_event(
        "celery.task.retried",
        level=logging.WARNING,
        task=task_name,
        reason=type(reason).__name__ if reason else "retry",
    )
