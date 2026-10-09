"""Tenant-scoped 99acres Push/Pull integration persistence."""
from __future__ import annotations

import uuid

from cryptography.fernet import InvalidToken
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.crypto import credential_cipher


def _cipher():
    return credential_cipher(purpose="shvya-99acres-v1")


class Acres99Integration(models.Model):
    class Mode(models.TextChoices):
        PUSH = "push", "Push (webhook)"
        PULL = "pull", "Pull (scheduled)"
        BOTH = "both", "Push + Pull"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization", on_delete=models.CASCADE,
        related_name="acres99_integration",
    )
    pipeline = models.ForeignKey(
        "crm.Pipeline", on_delete=models.RESTRICT, null=True, blank=True,
        related_name="acres99_integrations",
    )
    stage = models.ForeignKey(
        "crm.Stage", on_delete=models.RESTRICT, null=True, blank=True,
        related_name="acres99_integrations",
    )
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.PUSH)
    is_enabled = models.BooleanField(default=False)
    webhook_token = models.UUIDField(unique=True, null=True, blank=True, editable=False)
    encrypted_username = models.TextField(blank=True)
    encrypted_password = models.TextField(blank=True)
    encrypted_api_token = models.TextField(blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    last_received_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    sync_cursor = models.DateTimeField(null=True, blank=True)
    last_poll_at = models.DateTimeField(null=True, blank=True)
    poll_hour_start = models.DateTimeField(null=True, blank=True)
    poll_hour_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    received_count = models.PositiveBigIntegerField(default=0)
    created_count = models.PositiveBigIntegerField(default=0)
    linked_count = models.PositiveBigIntegerField(default=0)
    failed_count = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization__name"]
        verbose_name = "99acres Integration"

    def __str__(self):
        return f"99acres - {self.organization.name}"

    @property
    def has_credentials(self):
        return bool(self.encrypted_username and self.encrypted_password)

    @property
    def connection_state(self):
        if not self.is_enabled:
            return "paused" if self.provisioned_at else "requested"
        if self.last_received_at or self.last_synced_at:
            return "active"
        return "ready"

    def generate_webhook_token(self):
        self.webhook_token = uuid.uuid4()
        self.provisioned_at = timezone.now()

    @property
    def has_provider_token(self):
        return bool(self.encrypted_api_token)

    def set_provider_token(self, value):
        """Store the opaque /getmy99Response/<token>/uid/ segment privately."""
        import re
        value = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
            raise ValidationError("99acres Pull token must be 8-128 letters, digits, '-' or '_'.")
        self.encrypted_api_token = _cipher().encrypt(value.encode()).decode("ascii")

    def get_provider_token(self):
        if not self.encrypted_api_token:
            return None
        try:
            return _cipher().decrypt(self.encrypted_api_token.encode()).decode()
        except (InvalidToken, TypeError, ValueError, UnicodeDecodeError):
            return None

    def set_credentials(self, username, password):
        username, password = str(username or "").strip(), str(password or "")
        if not username or not password:
            raise ValidationError("Both 99acres account username and password are required.")
        cipher = _cipher()
        self.encrypted_username = cipher.encrypt(username.encode()).decode("ascii")
        self.encrypted_password = cipher.encrypt(password.encode()).decode("ascii")

    def get_credentials(self):
        if not self.has_credentials:
            return None
        try:
            cipher = _cipher()
            return (
                cipher.decrypt(self.encrypted_username.encode()).decode(),
                cipher.decrypt(self.encrypted_password.encode()).decode(),
            )
        except (InvalidToken, TypeError, ValueError, UnicodeDecodeError):
            return None

    def clean(self):
        super().clean()
        if self.pipeline_id and self.organization_id:
            if self.pipeline.organization_id != self.organization_id:
                raise ValidationError({"pipeline": "Pipeline belongs to another organization."})
        if self.stage_id and self.pipeline_id and self.stage.pipeline_id != self.pipeline_id:
            raise ValidationError({"stage": "Stage must belong to the selected pipeline."})
        if self.is_enabled:
            if not self.pipeline_id or not self.stage_id:
                raise ValidationError("Select a destination pipeline and stage.")
            if self.mode in {self.Mode.PUSH, self.Mode.BOTH} and not self.webhook_token:
                raise ValidationError("Provision a webhook token before enabling Push.")
            if self.mode in {self.Mode.PULL, self.Mode.BOTH} and not self.has_credentials:
                raise ValidationError("Configure encrypted 99acres credentials before enabling Pull.")
            if self.mode in {self.Mode.PULL, self.Mode.BOTH} and not self.has_provider_token:
                raise ValidationError("Configure the provider-specific 99acres Pull endpoint token.")


class Acres99Receipt(models.Model):
    """Successful imports only: the unique query ID survives lead deletion."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    integration = models.ForeignKey(
        Acres99Integration, on_delete=models.CASCADE, related_name="receipts",
    )
    external_query_id = models.CharField(max_length=180)
    direction = models.CharField(max_length=4, choices=[("push", "Push"), ("pull", "Pull")])
    property_id = models.CharField(max_length=120, blank=True)
    lead = models.ForeignKey("crm.Lead", on_delete=models.SET_NULL, null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "external_query_id"],
                name="uniq_acres99_external_query",
            ),
        ]
        indexes = [
            models.Index(fields=["integration", "received_at"], name="acres99_receipt_time"),
        ]


class Acres99Event(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        LINKED = "linked", "Existing contact"
        DUPLICATE = "duplicate", "Duplicate"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    integration = models.ForeignKey(
        Acres99Integration, on_delete=models.CASCADE, related_name="events",
    )
    external_query_id = models.CharField(max_length=180, blank=True)
    direction = models.CharField(max_length=4, choices=[("push", "Push"), ("pull", "Pull")])
    status = models.CharField(max_length=12, choices=Status.choices)
    lead = models.ForeignKey("crm.Lead", on_delete=models.SET_NULL, null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["integration", "created_at"], name="acres99_event_time"),
        ]
