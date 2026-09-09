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

# Customer-facing WhatsApp AI must not wait behind conversation summaries,
# ingestion, follow-ups, or other long-running default-queue work. Meta API
# engagement and Hosted Account AI each get an isolated production lane.
app.conf.task_routes = {
    "ai.generate_ai_engagement_response": {
        "queue": "ai_realtime",
    },
    "hosted.dispatch_due_ai": {
        "queue": "hosted_ai",
    },
    "apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task": {
        "queue": "hosted_ai",
    },
}

# Central Beat schedule for recurring background work. Hosted WhatsApp
# automation is intentionally evaluated every 10 seconds as a recovery scan.
# Each Hosted AI job also self-schedules a due-time wake-up, so Beat is no
# longer the only mechanism that can move a queued AI job into processing.
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
    "dispatch-ai-bump-ups-every-minute": {
        "task": "ai.dispatch_bump_ups",
        "schedule": 60.0,
    },
}
