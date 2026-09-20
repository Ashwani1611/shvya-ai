import uuid

from django.conf import settings
from django.db import models, transaction

from .lead import Lead


class LeadReminderQuerySet(models.QuerySet):
    def create(self, **kwargs):
        lead = kwargs.get("lead")
        lead_id = kwargs.get("lead_id") or getattr(lead, "pk", None)
        if not lead_id:
            return super().create(**kwargs)

        with transaction.atomic(using=self.db):
            Lead.objects.using(self.db).select_for_update().only("pk").get(pk=lead_id)
            self.model._base_manager.using(self.db).filter(lead_id=lead_id).delete()
            return super().create(**kwargs)


class LeadReminderManager(models.Manager.from_queryset(LeadReminderQuerySet)):
    pass


class LeadReminder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="reminders",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_at = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
        ],
        default="pending",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = LeadReminderManager()

    class Meta:
        ordering = ["due_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["lead"],
                name="uniq_lead_reminder_per_lead",
            )
        ]

    def __str__(self):
        return f"{self.title} — {self.lead.name}"

class LeadReminderNotificationAck(models.Model):
    """Per-user acknowledgement for a reminder notification popup.

    Acknowledging the popup does not complete, snooze, edit, or delete the
    underlying LeadReminder. It only records that this dashboard user has clicked
    the notification so the persistent popup can stop for that user.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reminder = models.ForeignKey(
        LeadReminder,
        on_delete=models.CASCADE,
        related_name="notification_acknowledgements",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reminder_notification_acknowledgements",
    )
    acknowledged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reminder", "user"],
                name="uniq_reminder_ack_user",
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "acknowledged_at"],
                name="crm_rem_ack_user_at_idx",
            )
        ]

    def __str__(self):
        return f"{self.user_id} acknowledged {self.reminder_id}"
