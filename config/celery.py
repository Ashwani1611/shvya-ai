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

# Register queue/task execution metrics after Django settings are available.
from config import celery_observability  # noqa: E402,F401

# Customer-facing WhatsApp AI must not wait behind conversation summaries,
# ingestion, follow-ups, or other long-running default-queue work. Meta API
# engagement and its final single-message delivery share the realtime lane;
# Hosted Account AI keeps its own isolated production lane.
# Routes are defined in settings so workers, tests and observability all read
# one canonical map. ``config_from_object`` has already loaded them here.

# Central Beat schedule for recurring background work. Each Hosted AI job
# self-schedules a due-time wake-up, and the dedicated recovery scans catch jobs
# or sender publications that were missed around a deploy/broker interruption.
app.conf.beat_schedule = {
    "call-intelligence-recovery": {
        "task": "apps.telephony.tasks.recover_call_intelligence", "schedule": 60.0,
    },
    "meta-conversions-outbox": {"task": "integrations.recover_meta_conversions", "schedule": 30.0},
    "signup-verification-delivery": {"task": "accounts.deliver_signup_verifications", "schedule": 60.0},
    "cleanup-deleted-organizations": {"task": "organizations.cleanup_deleted", "schedule": 60.0},
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
    "reconcile-hosted-sessions-every-30-seconds": {
        "task": "apps.channels.reconcile_hosted_sessions",
        "schedule": 30.0,
    },
    "dispatch-ai-bump-ups-every-minute": {
        "task": "ai.dispatch_bump_ups",
        "schedule": 60.0,
    },
    "reconcile-ai-credit-settlements-every-minute": {
        "task": "ai.reconcile_credit_settlements",
        "schedule": 60.0,
    },
    "refresh-instagram-tokens-every-6-hours": {
        "task": "apps.channels.instagram_tasks.refresh_instagram_tokens_task",
        "schedule": 21600.0,
    },
    "recover-instagram-webhooks-every-minute": {
        "task": "apps.channels.instagram_tasks.recover_instagram_webhook_deliveries_task",
        "schedule": 60.0,
    },
    # Support work uses the existing general worker, never the realtime AI lanes.
    "support-email-outbox": {
        "task": "support.deliver_notifications",
        "schedule": 30.0,
    },
    "support-maintenance": {
        "task": "support.maintain_tickets",
        "schedule": 60.0,
    },
    "support-mailbox": {
        "task": "support.poll_mailbox",
        "schedule": 60.0,
    },
    "sales-scheduled-delivery": {
        "task": "sales.dispatch_scheduled",
        "schedule": 30.0,
    },
    "sales-document-maintenance": {
        "task": "sales.maintain_documents",
        "schedule": 60.0,
    },
    "shvya-calendar-reminders": {
        "task": "shvya_calendar.dispatch_due_reminders",
        "schedule": 30.0,
    },
    "shvya-calendar-google-changes": {
        "task": "shvya_calendar.import_google_changes",
        "schedule": 60.0,
    },
    "shvya-calendar-google-meet-recovery": {
        "task": "shvya_calendar.recover_pending_google_meet",
        "schedule": 30.0,
    },
}
