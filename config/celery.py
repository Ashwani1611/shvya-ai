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
# engagement and its final single-message delivery share the realtime lane;
# Hosted Account AI keeps its own isolated production lane.
app.conf.task_routes = {
    "ai.recover_api_engagement": {"queue": "ai_realtime"},
    "ai.generate_ai_engagement_response": {
        "queue": "ai_realtime",
    },
    "apps.channels.tasks.send_whatsapp_message_task": {
        "queue": "ai_realtime",
    },
    "hosted.dispatch_due_ai": {
        "queue": "hosted_ai",
    },
    "apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task": {
        "queue": "hosted_ai",
    },
}

# Central Beat schedule for recurring background work. Each Hosted AI job
# self-schedules a due-time wake-up, and the dedicated recovery scans catch jobs
# or sender publications that were missed around a deploy/broker interruption.
app.conf.beat_schedule = {
    "recover-api-ai-every-10-seconds": {"task": "ai.recover_api_engagement", "schedule": 10.0},
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
    "dispatch-hosted-ai-recovery-every-5-seconds": {
        "task": "hosted.dispatch_due_ai",
        "schedule": 5.0,
    },
    "dispatch-ai-bump-ups-every-minute": {
        "task": "ai.dispatch_bump_ups",
        "schedule": 60.0,
    },
}
