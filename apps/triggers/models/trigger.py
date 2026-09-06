import uuid

from django.conf import settings
from django.db import models


class SmartTrigger(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=255)
    enabled = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)
    trigger_type = models.CharField(max_length=32)
    conditions = models.JSONField(default=dict)
    action_type = models.CharField(max_length=32)
    action = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "fingerprint"], name="st_org_fingerprint_unique"
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "enabled", "trigger_type"],
                name="st_org_event_idx",
            )
        ]


class TriggerEvent(models.Model):
    """Transactional outbox: broker outages never lose CRM events."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE
    )
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE)
    kind = models.CharField(max_length=32)
    key = models.CharField(max_length=255, unique=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["processed_at", "created_at"], name="st_pending_event_idx"
            ),
            models.Index(
                fields=["lead", "kind", "created_at"], name="st_lead_clock_idx"
            ),
        ]


class TriggerRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rule = models.ForeignKey(
        SmartTrigger, on_delete=models.CASCADE, related_name="runs"
    )
    event = models.ForeignKey(TriggerEvent, on_delete=models.CASCADE)
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE)
    action_type = models.CharField(max_length=32)
    action = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="pending")
    detail = models.TextField(blank=True)
    due_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    message = models.ForeignKey(
        "channels.WhatsAppMessage", null=True, on_delete=models.SET_NULL
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "event"], name="st_rule_event_unique"
            )
        ]
        indexes = [
            models.Index(fields=["status", "due_at"], name="st_due_run_idx"),
            models.Index(fields=["rule", "lead", "created_at"], name="st_cooldown_idx"),
        ]
