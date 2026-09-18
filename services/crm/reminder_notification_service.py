from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.crm.models import LeadReminder, LeadReminderNotificationAck


DEFAULT_NOTIFICATION_LIMIT = 5


def due_reminder_notifications(*, user, limit=DEFAULT_NOTIFICATION_LIMIT):
    """Return due, pending, unacknowledged reminders visible to this CRM user.

    The existing Reminders modal is organization-wide, so notification visibility
    deliberately mirrors that surface. Acknowledgement is per-user, which means
    one teammate clicking a popup never suppresses another teammate's notification.
    """
    organization_id = getattr(user, "organization_id", None)
    if not organization_id:
        return []

    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = DEFAULT_NOTIFICATION_LIMIT

    now = timezone.now()
    return list(
        LeadReminder.objects.filter(
            lead__organization_id=organization_id,
            status="pending",
            due_at__lte=now,
        )
        .exclude(notification_acknowledgements__user=user)
        .select_related("lead", "lead__pipeline", "lead__stage", "assigned_to")
        .order_by("due_at", "created_at")[:limit]
    )


def acknowledge_reminder_notification(*, user, reminder):
    """Acknowledge only the popup for this user; keep the reminder pending."""
    if getattr(user, "organization_id", None) != reminder.lead.organization_id:
        raise ValueError("Reminder does not belong to this user's organization.")

    with transaction.atomic():
        acknowledgement, _ = LeadReminderNotificationAck.objects.get_or_create(
            reminder=reminder,
            user=user,
        )
    return acknowledgement


def reset_reminder_notification_acknowledgements(*, reminder):
    """Make a snoozed/edited reminder eligible to notify users again when due."""
    return LeadReminderNotificationAck.objects.filter(reminder=reminder).delete()[0]
