"""Explicit operator requests to repair existing organization knowledge."""
import uuid
from django.db import models


class KnowledgeRepairRequest(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        DISPATCH_FAILED = "dispatch_failed", "Dispatch failed"
        RUNNING = "running", "Running"
        RETRYING = "retrying", "Retrying"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE)
    document = models.ForeignKey("ai_engagement.Document", on_delete=models.CASCADE)
    source = models.ForeignKey("ai_engagement.KnowledgeSource", null=True, blank=True, on_delete=models.SET_NULL)
    fingerprint = models.CharField(max_length=64)
    operation = models.CharField(max_length=24)
    task_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    attempt = models.PositiveSmallIntegerField(default=0)
    outcome_code = models.CharField(max_length=64, blank=True)
    result_document_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["organization", "document", "fingerprint"], name="ai_repair_document_revision_uniq")]
        indexes = [models.Index(fields=["organization", "status"], name="ai_repair_org_status_idx")]
