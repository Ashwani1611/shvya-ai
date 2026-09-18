"""Durable tenant-scoped action receipts and explainable conversation signals."""
from django.core.exceptions import ValidationError
from django.db import models


class AIActionReceipt(models.Model):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE)
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE)
    source_message_id = models.UUIDField()
    idempotency_key = models.CharField(max_length=64)
    action_type = models.CharField(max_length=40)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["organization", "idempotency_key"], name="ai_receipt_org_key_unique",
        )]
        indexes = [models.Index(
            fields=["organization", "lead", "source_message_id"], name="ai_receipt_org_lead_source",
        )]

    def clean(self):
        if self.lead_id and self.lead.organization_id != self.organization_id:
            raise ValidationError("Action receipt lead is outside organization scope.")


class LeadSignal(models.Model):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE)
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE)
    source_message_id = models.UUIDField()
    signal = models.CharField(max_length=64)
    value = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(default=1.0)
    evidence = models.CharField(max_length=500, blank=True)
    observed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["organization", "lead", "source_message_id", "signal"],
            name="ai_signal_source_kind_unique",
        )]
        indexes = [
            models.Index(fields=["organization", "lead", "-observed_at"], name="ai_signal_org_lead_time"),
            models.Index(fields=["organization", "signal", "-observed_at"], name="ai_signal_org_kind_time"),
        ]

    def clean(self):
        if self.lead_id and self.lead.organization_id != self.organization_id:
            raise ValidationError("Signal lead is outside organization scope.")
