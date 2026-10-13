"""Compact durable evidence; never a replacement for domain-owned state."""

import uuid

from django.db import models
from django.conf import settings
from django.utils import timezone


class APIActivity(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        ACCEPTED = "accepted", "Accepted"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        UNKNOWN = "unknown", "Unknown outcome"
        BLOCKED = "blocked", "Blocked"
        SKIPPED = "skipped", "Skipped"
        PARTIAL = "partial", "Partially completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correlation_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    parent_id = models.UUIDField(null=True, blank=True)
    organization = models.ForeignKey(
        "organizations.Organization", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="api_activities",
    )
    organization_reference = models.CharField(max_length=64, blank=True)
    integration = models.CharField(max_length=48, default="shvya")
    provider = models.CharField(max_length=48, blank=True)
    kind = models.CharField(max_length=48, default="api")
    direction = models.CharField(max_length=16, blank=True)
    operation = models.CharField(max_length=160, blank=True)
    connection_id = models.CharField(max_length=80, blank=True)
    entity_type = models.CharField(max_length=64, blank=True)
    entity_id = models.CharField(max_length=160, blank=True)
    request_id = models.CharField(max_length=80, blank=True)
    task_id = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROCESSING)
    method = models.CharField(max_length=16, blank=True)
    endpoint = models.CharField(max_length=500, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    provider_reference = models.CharField(max_length=160, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_category = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    summary = models.CharField(max_length=1000, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    expected_by = models.DateTimeField(null=True, blank=True)
    next_step = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("organization", "-created_at"), name="api_act_org_time_idx"),
            models.Index(fields=("integration", "-created_at"), name="api_act_int_time_idx"),
            models.Index(fields=("status", "-created_at"), name="api_act_status_time_idx"),
            models.Index(fields=("-created_at",), name="api_act_created_idx"),
            models.Index(fields=("expected_by",), name="api_act_expected_idx"),
            models.Index(fields=("organization", "entity_type", "entity_id"), name="api_act_entity_idx"),
            models.Index(fields=("organization", "provider_reference"), name="api_act_provider_ref_idx"),
            models.Index(fields=("organization", "parent_id"), name="api_act_org_parent_idx"),
        ]

    def __str__(self):
        return f"{self.integration}: {self.operation} ({self.status})"


class APIActivityEvent(models.Model):
    activity = models.ForeignKey(APIActivity, on_delete=models.CASCADE, related_name="events")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    name = models.CharField(max_length=80)
    status = models.CharField(max_length=20, choices=APIActivity.Status.choices, blank=True)
    summary = models.CharField(max_length=1000, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("timestamp", "id")


class APIIncident(models.Model):
    """Human investigation state, independent of provider/business outcomes."""

    activity = models.OneToOneField(APIActivity, on_delete=models.CASCADE, related_name="incident")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=16, default="open", choices=[
        ("open", "Open"), ("acknowledged", "Acknowledged"), ("resolved", "Resolved"),
    ])
    severity = models.CharField(max_length=12, default="medium", choices=[
        ("low", "Low"), ("medium", "Medium"), ("high", "High"),
    ])
    due_at = models.DateTimeField(null=True, blank=True)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("status", "due_at"), name="api_inc_status_due_idx")]


class APIIncidentEvent(models.Model):
    incident = models.ForeignKey(APIIncident, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=24)
    note = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
