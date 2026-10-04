"""Tenant-owned Meta credentials, stage mappings and durable event outbox."""

import uuid

from cryptography.fernet import InvalidToken
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.crypto import credential_cipher


class MetaConversionsConfiguration(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization", on_delete=models.CASCADE,
        related_name="meta_conversions_configuration",
    )
    dataset_id = models.CharField(max_length=30, blank=True)
    encrypted_access_token = models.TextField(blank=True)
    destination_version = models.UUIDField(default=uuid.uuid4, editable=False)
    is_enabled = models.BooleanField(default=False)
    test_mode = models.BooleanField(default=True)
    test_event_code = models.CharField(max_length=100, blank=True)
    event_scope = models.CharField(
        max_length=20, default="meta_leads",
        choices=[("meta_leads", "Meta Lead Ads only"), ("all_leads", "All lead sources")],
    )
    user_data_fields = models.JSONField(
        default=list, blank=True,
    )
    custom_attribute_keys = models.JSONField(default=list, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def has_access_token(self):
        return bool(self.encrypted_access_token)

    def set_access_token(self, token):
        cipher = credential_cipher(purpose="shvya-meta-conversions-v1")
        self.encrypted_access_token = (
            cipher.encrypt(token.encode()).decode("ascii") if token else ""
        )

    def get_access_token(self):
        if not self.encrypted_access_token:
            return ""
        try:
            return credential_cipher(purpose="shvya-meta-conversions-v1").decrypt(
                self.encrypted_access_token.encode("ascii")
            ).decode()
        except (InvalidToken, ValueError, TypeError):
            return ""


class MetaConversionMapping(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    configuration = models.ForeignKey(
        MetaConversionsConfiguration, on_delete=models.CASCADE, related_name="mappings",
    )
    pipeline = models.ForeignKey("crm.Pipeline", on_delete=models.CASCADE)
    stage = models.ForeignKey("crm.Stage", on_delete=models.CASCADE)
    event_name = models.CharField(max_length=100, default="Lead")
    is_enabled = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    action_source = models.CharField(max_length=30, default="system_generated")
    value_source = models.CharField(
        max_length=12, default="none",
        choices=[("none", "No value"), ("static", "Static"), ("attribute", "Attribute")],
    )
    static_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    value_attribute = models.CharField(max_length=100, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pipeline__name", "stage__display_order", "created_at"]
        constraints = [models.UniqueConstraint(
            fields=["configuration", "stage"], name="uniq_meta_conversion_stage",
        )]

    def clean(self):
        super().clean()
        if self.pipeline_id and self.configuration_id:
            if self.pipeline.organization_id != self.configuration.organization_id:
                raise ValidationError({"pipeline": "Choose a pipeline in your organization."})
        if self.stage_id and self.pipeline_id and self.stage.pipeline_id != self.pipeline_id:
            raise ValidationError({"stage": "Choose a stage in the selected pipeline."})


class MetaConversionDelivery(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        RETRYING = "retrying", "Retrying"
        SENT = "sent", "Accepted by Meta"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    configuration = models.ForeignKey(
        MetaConversionsConfiguration, on_delete=models.CASCADE, related_name="deliveries",
    )
    mapping = models.ForeignKey(
        MetaConversionMapping, null=True, on_delete=models.SET_NULL, related_name="deliveries",
    )
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE)
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE)
    event_id = models.CharField(max_length=100)
    dataset_id = models.CharField(max_length=30)
    destination_version = models.UUIDField()
    payload = models.JSONField(default=dict)
    is_test = models.BooleanField(default=False)
    is_probe = models.BooleanField(default=False)
    test_event_code = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_enqueued_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    meta_error_code = models.PositiveIntegerField(null=True, blank=True)
    trace_id = models.CharField(max_length=100, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(
            fields=["configuration", "event_id"], name="uniq_meta_conversion_event",
        )]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="meta_capi_delivery_due"),
            models.Index(fields=["organization", "created_at"], name="meta_capi_org_created"),
        ]
