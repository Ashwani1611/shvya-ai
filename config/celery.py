"""Celery configuration for SHVYA AI."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("shvya")

app.config_from_object(
    "django.conf:settings",
    namespace="CELERY",
)

app.autodiscover_tasks()

# Central Beat schedule for recurring background work. The Auto Follow-ups
# dispatcher intentionally runs frequently but processes at most one due lead
# per invocation; service-layer locking and per-sender throttling prevent a
# burst where every assigned lead is sent at once.
app.conf.beat_schedule = {
    "dispatch-smart-triggers-every-10-seconds": {
        "task": "apps.triggers.tasks.dispatch_smart_triggers",
        "schedule": 10.0,
    },
    **(app.conf.beat_schedule or {}),
    "refresh-copilot-flags-every-30-minutes": {
        "task": "apps.copilot.tasks.refresh_copilot_flags_task",
        "schedule": 1800.0,
    },
    "dispatch-auto-followups-every-10-seconds": {
        "task": "apps.followups.tasks.dispatch_auto_followups_task",
        "schedule": 10.0,
    },
}
