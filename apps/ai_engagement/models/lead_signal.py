from django.db import models


class LeadSignal(models.Model):
    """Individual source-backed observations, never a second qualification state."""
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE,
                                     related_name="ai_lead_signals")
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE, related_name="ai_signals")
    source_message_id = models.UUIDField()
    kind = models.CharField(max_length=40)
    detail = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["organization", "lead", "source_message_id", "kind", "detail"],
            name="uniq_ai_lead_signal",
        )]
        indexes = [models.Index(fields=["organization", "lead", "kind"], name="ai_lead_signal_kind_idx")]
