from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.organizations.models import Organization


class JustDialIntegration(models.Model):
    """Organization-scoped JustDial lead-push configuration.

    The public webhook token is deliberately provisioned only through the
    Superadmin workflow. Organization admins can request setup and view/copy an
    already-provisioned URL, but they cannot mint or rotate the token.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="justdial_integration",
    )
    pipeline = models.ForeignKey(
        "crm.Pipeline",
        on_delete=models.RESTRICT,
        related_name="justdial_integrations",
        null=True,
        blank=True,
    )
    stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.RESTRICT,
        related_name="justdial_integrations",
        null=True,
        blank=True,
    )
    webhook_token = models.UUIDField(
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )
    is_enabled = models.BooleanField(default=False)
    requested_at = models.DateTimeField(default=timezone.now)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    last_received_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    received_count = models.PositiveBigIntegerField(default=0)
    created_count = models.PositiveBigIntegerField(default=0)
    updated_count = models.PositiveBigIntegerField(default=0)
    ignored_count = models.PositiveBigIntegerField(default=0)
    error_count = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization__name"]
        verbose_name = "JustDial Integration"
        verbose_name_plural = "JustDial Integrations"

    def __str__(self):
        return f"JustDial - {self.organization.name}"

    @property
    def is_provisioned(self):
        return bool(self.webhook_token and self.pipeline_id and self.stage_id)

    @property
    def connection_state(self):
        if not self.webhook_token:
            return "requested"
        if not self.is_enabled:
            return "paused"
        if self.last_received_at:
            return "active"
        return "ready"

    def generate_webhook_token(self):
        self.webhook_token = uuid.uuid4()
        self.provisioned_at = timezone.now()

    def clean(self):
        super().clean()

        if self.organization_id and self.pipeline_id:
            if self.pipeline.organization_id != self.organization_id:
                raise ValidationError(
                    {"pipeline": "Pipeline does not belong to this organization."}
                )

        if self.pipeline_id and self.stage_id:
            if self.stage.pipeline_id != self.pipeline_id:
                raise ValidationError(
                    {"stage": "Stage does not belong to the selected pipeline."}
                )

        if self.is_enabled and not self.webhook_token:
            raise ValidationError(
                {"is_enabled": "Generate a JustDial webhook before enabling it."}
            )
        if self.is_enabled and (not self.pipeline_id or not self.stage_id):
            raise ValidationError(
                {"is_enabled": "Choose a destination pipeline and stage first."}
            )


class JustDialLeadEvent(models.Model):
    """Audit record for every JustDial lead-push request SHVYA accepts."""

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    integration = models.ForeignKey(
        JustDialIntegration,
        on_delete=models.CASCADE,
        related_name="events",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="justdial_events",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        related_name="justdial_events",
        null=True,
        blank=True,
    )
    external_lead_id = models.CharField(max_length=160, blank=True)
    method = models.CharField(max_length=8, default="GET")
    status = models.CharField(max_length=12, choices=Status.choices)
    payload = models.JSONField(default=dict, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "status", "created_at"],
                name="justdial_org_status_created",
            ),
            models.Index(
                fields=["integration", "external_lead_id"],
                name="justdial_integration_leadid",
            ),
        ]
        verbose_name = "JustDial Lead Event"
        verbose_name_plural = "JustDial Lead Events"

    def __str__(self):
        return f"{self.external_lead_id or self.id} ({self.status})"
