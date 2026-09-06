import uuid

from django.db import models


class HostedAccountHealth(models.Model):
    """Durable protection state for one hosted WhatsApp account."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(
        "channels.WhatsAppAccount",
        on_delete=models.CASCADE,
        related_name="hosted_health",
    )
    enabled = models.BooleanField(default=True)
    total_messages_sent = models.PositiveBigIntegerField(default=0)
    window_messages_sent = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField(null=True, blank=True)
    paused_until = models.DateTimeField(null=True, blank=True, db_index=True)
    last_followup_sent_at = models.DateTimeField(null=True, blank=True)
    last_followup_content_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Hosted health — {self.account}"


class HostedAutomationJob(models.Model):
    """Durable delayed work for hosted AI engagement."""

    class Kind(models.TextChoices):
        AI_ENGAGEMENT = "ai_engagement", "AI Engagement"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="hosted_automation_jobs",
    )
    account = models.ForeignKey(
        "channels.WhatsAppAccount",
        on_delete=models.CASCADE,
        related_name="hosted_automation_jobs",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.CASCADE,
        related_name="hosted_automation_jobs",
    )
    source_message = models.OneToOneField(
        "channels.WhatsAppMessage",
        on_delete=models.CASCADE,
        related_name="hosted_automation_job",
    )
    kind = models.CharField(
        max_length=24,
        choices=Kind.choices,
        default=Kind.AI_ENGAGEMENT,
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    available_at = models.DateTimeField(db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["available_at", "created_at"]
        indexes = [
            models.Index(
                fields=["account", "status", "available_at"],
                name="hosted_job_acc_due_idx",
            ),
            models.Index(
                fields=["organization", "status", "available_at"],
                name="hosted_job_org_due_idx",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.lead} — {self.status}"


class HostedFollowupStepConfig(models.Model):
    """Free-form WhatsApp content used only by hosted follow-up steps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    step = models.OneToOneField(
        "followups.FollowupStep",
        on_delete=models.CASCADE,
        related_name="hosted_config",
    )
    body = models.TextField()
    attachment = models.FileField(
        upload_to="followups/hosted/%Y/%m/%d/",
        blank=True,
    )
    attachment_original_name = models.CharField(max_length=255, blank=True)
    attachment_mime_type = models.CharField(max_length=120, blank=True)
    attachment_size = models.PositiveBigIntegerField(default=0)
    authored_content_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Hosted WhatsApp — {self.step}"
