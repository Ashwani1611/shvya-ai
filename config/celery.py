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

# Co-Pilot stores a cached signal table and refreshes it every 30 minutes.
# Keeping this schedule here avoids duplicating it across local/production
# settings modules. A celery beat process is provided by docker-compose.
app.conf.beat_schedule = {
    **(app.conf.beat_schedule or {}),
    "refresh-copilot-flags-every-30-minutes": {
        "task": "apps.copilot.tasks.refresh_copilot_flags_task",
        "schedule": 1800.0,
    },
}
