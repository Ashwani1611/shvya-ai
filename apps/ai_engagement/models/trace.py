import uuid

from django.db import models

from apps.crm.models import Lead
from apps.organizations.models import Organization


class AITrace(models.Model):
    class Status(models.TextChoices):
        STARTED = "started", "Started"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        BLOCKED = "blocked", "Blocked"
        FAILED = "failed", "Failed"
        STALE = "stale", "Stale"
        DUPLICATE = "duplicate", "Duplicate"
        SILENCED = "silenced", "Silenced"

    class ConnectionType(models.TextChoices):
        API = "api", "API"
        HOSTED = "hosted", "Hosted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="ai_traces",
    )
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="ai_traces",
    )
    pipeline_id = models.UUIDField(null=True, blank=True)
    stage_id = models.UUIDField(null=True, blank=True)
    whatsapp_account_id = models.UUIDField(null=True, blank=True)
    source_inbound_message_id = models.UUIDField(db_index=True)
    outbound_message_id = models.UUIDField(null=True, blank=True)
    connection_type = models.CharField(max_length=16, choices=ConnectionType.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.STARTED)
    reason_code = models.CharField(max_length=96, blank=True, db_index=True)
    execution_path = models.CharField(max_length=32, blank=True)
    model_name = models.CharField(max_length=150, blank=True)
    incoming_message_preview = models.CharField(max_length=500, blank=True)
    response_preview = models.CharField(max_length=500, blank=True)
    total_ms = models.PositiveIntegerField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["organization", "-started_at"], name="ai_trace_org_created_idx"),
            models.Index(fields=["organization", "lead", "-started_at"], name="ai_trace_org_lead_idx"),
            models.Index(fields=["organization", "status", "-started_at"], name="ai_trace_org_status_idx"),
            models.Index(fields=["organization", "connection_type", "-started_at"], name="ai_trace_org_conn_idx"),
        ]

    def __str__(self):
        return f"AI Trace {self.id} - {self.status}"
