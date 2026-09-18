"""Durable CRM action receipts; written in the same transaction as the mutation."""
import uuid

from django.db import models


class AIActionReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE,
                                     related_name="ai_action_receipts")
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE, related_name="ai_action_receipts")
    source_message_id = models.UUIDField()
    idempotency_key = models.CharField(max_length=64)
    action_type = models.CharField(max_length=32)
    # IDs/status only. Never store full note text, model prompts or credentials.
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["organization", "lead", "idempotency_key"], name="uniq_ai_action_receipt")]
        indexes = [models.Index(fields=["organization", "lead", "source_message_id"],
                                name="ai_action_source_idx")]
