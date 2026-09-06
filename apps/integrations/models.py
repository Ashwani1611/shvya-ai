import base64
import hashlib
import uuid

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

from apps.organizations.models import Organization


def _webhook_fernet():
    """Return a stable Fernet instance derived from Django's SECRET_KEY."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class WebhookConfiguration(models.Model):
    """Organization-scoped outbound lead webhook configuration."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="webhook_configuration",
    )
    endpoint_url = models.URLField(
        max_length=2048,
        blank=True,
    )
    encrypted_secret = models.TextField(
        blank=True,
    )
    is_enabled = models.BooleanField(
        default=False,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["organization__name"]
        verbose_name = "Webhook Configuration"
        verbose_name_plural = "Webhook Configurations"

    def __str__(self):
        return f"Webhook - {self.organization.name}"

    @property
    def has_secret(self):
        return bool(self.encrypted_secret)

    def set_secret(self, raw_secret):
        raw_secret = str(raw_secret or "")
        self.encrypted_secret = (
            _webhook_fernet().encrypt(raw_secret.encode("utf-8")).decode("ascii")
            if raw_secret
            else ""
        )

    def get_secret(self):
        if not self.encrypted_secret:
            return ""

        try:
            return _webhook_fernet().decrypt(
                self.encrypted_secret.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""


class WebhookDelivery(models.Model):
    """Immutable payload plus delivery state for one outbound lead event."""

    class EventType(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RETRYING = "retrying", "Retrying"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    webhook = models.ForeignKey(
        WebhookConfiguration,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="webhook_deliveries",
    )
    lead_id = models.UUIDField()
    event_type = models.CharField(
        max_length=10,
        choices=EventType.choices,
    )
    payload = models.JSONField(
        default=dict,
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    attempt_count = models.PositiveSmallIntegerField(
        default=0,
    )
    response_status = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )
    response_body = models.TextField(
        blank=True,
    )
    error_message = models.TextField(
        blank=True,
    )
    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "status", "created_at"],
                name="webhook_org_status_created",
            ),
            models.Index(
                fields=["lead_id", "created_at"],
                name="webhook_lead_created",
            ),
        ]
        verbose_name = "Webhook Delivery"
        verbose_name_plural = "Webhook Deliveries"

    def __str__(self):
        return f"{self.event_type} {self.lead_id} ({self.status})"
