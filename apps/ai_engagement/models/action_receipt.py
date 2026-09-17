"""Committed CRM outcomes for retry-safe AI actions; never model-owned state."""
import uuid

from django.core.exceptions import ValidationError
from django.db import models


class CRMActionReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE,
        related_name="ai_action_receipts",
    )
    lead = models.ForeignKey(
        "crm.Lead", on_delete=models.CASCADE, related_name="ai_action_receipts",
    )
    source_inbound_message_id = models.UUIDField()
    idempotency_key = models.CharField(max_length=64)
    action_type = models.CharField(max_length=32)
    # Store only canonical result IDs/status, not prompts, message bodies,
    # credentials, or model-proposed action parameters.
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "lead", "source_inbound_message_id", "idempotency_key"),
                name="ai_action_receipt_unique",
            ),
        ]

    def clean(self):
        super().clean()
        from apps.crm.models import Lead
        from apps.channels.models import WhatsAppMessage

        if not Lead.objects.filter(pk=self.lead_id, organization_id=self.organization_id).exists():
            raise ValidationError("Action receipt lead is outside organization scope.")
        if not WhatsAppMessage.objects.filter(
            pk=self.source_inbound_message_id, organization_id=self.organization_id,
            lead_id=self.lead_id, direction="inbound",
        ).exists():
            raise ValidationError("Action receipt source is outside lead scope.")
