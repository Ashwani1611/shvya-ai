from django.db import models

from apps.organizations.models import Organization


class KnowledgeSource(models.Model):
    """
    Organization-owned knowledge source.

    A source can currently be a URL or a file reference.
    The source is kept separate from OrgInfo so knowledge can
    grow without turning the organization AI configuration into
    a large JSON/blob field.
    """

    class SourceType(models.TextChoices):
        URL = "url", "Website URL"
        FILE = "file", "File"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="ai_knowledge_sources",
    )

    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
    )

    name = models.CharField(
        max_length=255,
        blank=True,
    )

    url = models.URLField(
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "-updated_at",
        ]

    def __str__(self):
        return (
            f"{self.organization.name} - "
            f"{self.get_source_type_display()} - "
            f"{self.name or self.url}"
        )