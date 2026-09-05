"""Operational metadata for WhatsApp message templates.

The existing WhatsAppTemplate remains the canonical template record. These
models extend it with Meta synchronization state and an immutable operation
trail without duplicating the template domain model.
"""

import uuid

from django.db import models


class WhatsAppTemplateMetadata(models.Model):
    """One-to-one Meta/local synchronization state for a WhatsAppTemplate."""

    class LocalStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTING = "submitting", "Submitting"
        SUBMITTED = "submitted", "Submitted"
        SYNCED = "synced", "Synced"
        SYNC_ERROR = "sync_error", "Sync Error"
        REMOTE_DELETED = "remote_deleted", "Not Found on Meta"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.OneToOneField(
        "channels.WhatsAppTemplate",
        on_delete=models.CASCADE,
        related_name="meta_state",
    )
    local_status = models.CharField(
        max_length=24,
        choices=LocalStatus.choices,
        default=LocalStatus.DRAFT,
        db_index=True,
    )
    language = models.CharField(max_length=16, default="en_US")
    placeholder_mapping = models.JSONField(default=dict, blank=True)
    components = models.JSONField(default=list, blank=True)
    meta_response = models.JSONField(default=dict, blank=True)
    meta_error_code = models.CharField(max_length=64, blank=True)
    meta_error_subcode = models.CharField(max_length=64, blank=True)
    meta_error_type = models.CharField(max_length=128, blank=True)
    meta_error_message = models.TextField(blank=True)

    # Meta media-header sample data. The handle is returned by Meta's
    # resumable-upload API and is safe to persist; raw file bytes are never
    # stored in the database.
    header_sample_handle = models.TextField(blank=True)
    header_file_name = models.CharField(max_length=255, blank=True)
    header_mime_type = models.CharField(max_length=128, blank=True)
    header_file_size = models.PositiveBigIntegerField(null=True, blank=True)

    last_synced_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["local_status", "last_synced_at"], name="wa_tpl_state_sync_idx"),
        ]

    def __str__(self):
        return f"{self.template.name} — {self.local_status}"


class WhatsAppTemplateOperation(models.Model):
    """Safe audit/debug record for real Meta template operations."""

    class Operation(models.TextChoices):
        SUBMIT = "submit", "Submit"
        SYNC = "sync", "Sync"
        DELETE = "delete", "Delete"
        COPY = "copy", "Copy"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="whatsapp_template_operations",
    )
    template = models.ForeignKey(
        "channels.WhatsAppTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operations",
    )
    account = models.ForeignKey(
        "channels.WhatsAppAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="template_operations",
    )
    operation = models.CharField(max_length=16, choices=Operation.choices)
    success = models.BooleanField(default=False)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    meta_error_code = models.CharField(max_length=64, blank=True)
    meta_error_subcode = models.CharField(max_length=64, blank=True)
    meta_error_type = models.CharField(max_length=128, blank=True)
    meta_error_message = models.TextField(blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"], name="wa_tpl_op_org_time_idx"),
            models.Index(fields=["template", "operation"], name="wa_tpl_op_tpl_kind_idx"),
        ]

    def __str__(self):
        return f"{self.operation} — {self.template_id or 'no-template'}"
