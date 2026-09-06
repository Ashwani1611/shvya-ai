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

# Central Beat schedule for recurring background work. Hosted WhatsApp
# automation is intentionally evaluated every 20 seconds. The dispatcher
# processes one prioritized lane at a time and the service layer provides
# durable account/lead throttling so queued work cannot fan out in a burst.
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
    "dispatch-auto-followups-every-20-seconds": {
        "task": "apps.followups.tasks.dispatch_auto_followups_task",
        "schedule": 20.0,
    },
}
