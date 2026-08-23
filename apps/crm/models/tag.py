import uuid

from django.db import models

from apps.organizations.models import Organization

from .lead import Lead


class Tag(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="crm_tags",
    )

    name = models.CharField(
        max_length=50,
    )

    color = models.CharField(
        max_length=20,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="uniq_org_tag_name",
            )
        ]

    def __str__(self):
        return self.name


class LeadTag(models.Model):
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="tags",
    )

    tag = models.ForeignKey(
        Tag,
        on_delete=models.CASCADE,
        related_name="leads",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lead", "tag"],
                name="uniq_lead_tag",
            )
        ]

    def __str__(self):
        return f"{self.lead.name} — {self.tag.name}"