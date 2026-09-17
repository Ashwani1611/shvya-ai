import uuid

from django.db import models

from apps.crm.models import Lead
from apps.organizations.models import Organization


class LeadMemory(models.Model):
    """Durable AI memory for one lead inside one organization.

    The identity is deliberately ``(organization, lead)``. Phone numbers and
    channel/account identifiers are never used as the memory key because the
    same person/number may legitimately interact with multiple SHVYA tenants.

    ``structured_facts`` stores provenance-aware facts. ``long_term_events`` is
    a bounded, compressed event history that complements, rather than replaces,
    ``InternalConversationSummary``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="lead_memories",
    )
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="ai_memories",
    )
    structured_facts = models.JSONField(default=dict, blank=True)
    long_term_events = models.JSONField(default=list, blank=True)
    source_last_message_id = models.UUIDField(null=True, blank=True)
    source_last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "lead"],
                name="uniq_ai_lead_memory_org_lead",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "lead"],
                name="ai_memory_org_lead_idx",
            ),
            models.Index(
                fields=["organization", "-updated_at"],
                name="ai_memory_org_updated_idx",
            ),
        ]

    def __str__(self):
        return f"Lead Memory - {self.organization_id}/{self.lead_id}"
