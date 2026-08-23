import uuid

from django.conf import settings
from django.db import models

from apps.organizations.models import Organization


class Pipeline(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="pipelines",
    )

    name = models.CharField(
        max_length=150,
    )

    description = models.TextField(
        blank=True,
    )

    country_code = models.CharField(
        max_length=10,
        blank=True,
    )

    phone_number = models.CharField(
        max_length=32,
        blank=True,
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_pipelines",
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
        ordering = ["name"]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "name",
                ],
                name="uniq_org_pipeline_name",
            )
        ]

    def __str__(self):
        return f"{self.organization.name} → {self.name}"