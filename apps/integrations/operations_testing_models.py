"""Durable server-owned fixtures and metering for Operations AI flow tests."""
import uuid

from django.conf import settings
from django.db import models


class OperationsAIFlowRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    fixture_lead_id = models.UUIDField(editable=False)
    fixture_account_id = models.UUIDField(editable=False)
    idempotency_key = models.CharField(max_length=100)
    input_digest = models.CharField(max_length=64)
    name = models.CharField(max_length=120)
    channel = models.CharField(max_length=30, default="whatsapp")
    status = models.CharField(max_length=20, default="ready")
    max_turns = models.PositiveIntegerField(default=30)
    max_provider_calls = models.PositiveIntegerField(default=100)
    max_credits = models.PositiveIntegerField(default=1000)
    provider_calls = models.PositiveIntegerField(default=0)
    active_turn_id = models.UUIDField(null=True, editable=False)
    configuration_digest = models.CharField(max_length=64, blank=True)
    manifest = models.JSONField(default=dict, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cleaned_at = models.DateTimeField(null=True)

    class Meta:
        app_label = "integrations"
        indexes = [models.Index(fields=["organization", "status"], name="ops_ai_flow_org_status")]
        constraints = [models.UniqueConstraint(fields=["organization", "idempotency_key"], name="ops_ai_flow_run_idempotency")]


class OperationsAIFlowTurn(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(OperationsAIFlowRun, on_delete=models.CASCADE, related_name="turns")
    idempotency_key = models.CharField(max_length=100)
    input_digest = models.CharField(max_length=64)
    message = models.TextField()
    status = models.CharField(max_length=20, default="running")
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        app_label = "integrations"
        constraints = [models.UniqueConstraint(fields=["run", "idempotency_key"], name="ops_ai_flow_turn_idempotency")]


class OperationsAIFlowReservation(models.Model):
    run = models.ForeignKey(OperationsAIFlowRun, on_delete=models.CASCADE, related_name="usage_reservations")
    reservation = models.OneToOneField("ai_engagement.AICreditReservation", on_delete=models.PROTECT)

    class Meta:
        app_label = "integrations"
