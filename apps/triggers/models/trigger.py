import uuid

from django.conf import settings
from django.db import models


class SmartTrigger(models.Model):
    """Organization-scoped automation rule evaluated from CRM events."""

    class EventType(models.TextChoices):
        LEAD_CREATED = "lead.created", "Lead created"
        LEAD_UPDATED = "lead.updated", "Lead updated"
        LEAD_STAGE_CHANGED = "lead.stage_changed", "Lead stage changed"
        WHATSAPP_RECEIVED = "whatsapp.received", "WhatsApp message received"

    class ConditionMode(models.TextChoices):
        ALL = "all", "Match all conditions"
        ANY = "any", "Match any condition"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="smart_triggers",
    )
    name = models.CharField(max_length=255)
    description = models.CharField(max_length=300, blank=True)
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    condition_mode = models.CharField(
        max_length=8,
        choices=ConditionMode.choices,
        default=ConditionMode.ALL,
    )
    conditions = models.JSONField(default=list, blank=True)
    actions = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    once_per_lead = models.BooleanField(default=False)
    cooldown_minutes = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_smart_triggers",
    )
    successful_runs = models.PositiveIntegerField(default=0)
    failed_runs = models.PositiveIntegerField(default=0)
    last_fired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="trigger_org_name_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "is_active", "event_type"],
                name="trigger_org_event_idx",
            ),
            models.Index(
                fields=["organization", "updated_at"],
                name="trigger_org_updated_idx",
            ),
        ]

    def __str__(self):
        return self.name


class TriggerExecution(models.Model):
    """Immutable-ish audit record for one trigger evaluating one event."""

    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="trigger_executions",
    )
    trigger = models.ForeignKey(
        SmartTrigger,
        on_delete=models.CASCADE,
        related_name="executions",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="trigger_executions",
    )
    event_id = models.UUIDField()
    event_type = models.CharField(max_length=32)
    event_payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices)
    matched = models.BooleanField(default=False)
    skip_reason = models.CharField(max_length=255, blank=True)
    action_results = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["trigger", "event_id"],
                name="trigger_event_once_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "status", "created_at"],
                name="trigger_exec_org_idx",
            ),
            models.Index(
                fields=["trigger", "created_at"],
                name="trigger_exec_rule_idx",
            ),
            models.Index(
                fields=["lead", "trigger", "created_at"],
                name="trigger_exec_lead_idx",
            ),
        ]

    def __str__(self):
        return f"{self.trigger.name} · {self.event_type} · {self.status}"
