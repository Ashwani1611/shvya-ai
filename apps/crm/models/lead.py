import re
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.organizations.models import Organization

from .pipeline import Pipeline
from .stage import Stage


def normalize_phone(phone):
    """
    SHVYA requires phone numbers with country code.

    Examples:
        +91-9876543210
        +91 9876543210
        +919876543210

    All are stored as:
        +919876543210
    """

    value = str(phone).strip()

    if not value.startswith("+"):
        raise ValidationError(
            {
                "phone": (
                    "Phone number must include country code, "
                    "for example +91-9876543210."
                )
            }
        )

    digits = re.sub(r"\D", "", value)

    if len(digits) < 8:
        raise ValidationError(
            {
                "phone": "Phone number is too short."
            }
        )

    return f"+{digits}"


class Lead(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="leads",
    )
    stage_entered_at = models.DateTimeField(
    default=timezone.now,
    )

    pipeline = models.ForeignKey(
        Pipeline,
        on_delete=models.CASCADE,
        related_name="leads",
    )

    stage = models.ForeignKey(
        Stage,
        on_delete=models.CASCADE,
        related_name="leads",
    )

    name = models.CharField(
        max_length=150,
    )

    phone = models.CharField(
        max_length=32,
    )

    email = models.EmailField(
        blank=True,
    )

    notes = models.TextField(
        blank=True,
    )

    attributes = models.JSONField(
        default=dict,
        blank=True,
    )
    ai_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Allow SHVYA AI to engage this lead."
        ),
    )

    lead_source = models.CharField(
    max_length=30,
    choices=[
        ("system", "System"),
        ("external_api", "External API"),
        ("whatsapp_api", "WhatsApp API"),
        ("google_sheets", "Google Sheets"),
        ("csv_import", "CSV Import"),
    ],
    default="system",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "phone",
                ],
                name="uniq_org_phone",
            )
        ]

        indexes = [
            models.Index(
                fields=["organization"],
            ),
            models.Index(
                fields=["pipeline"],
            ),
            models.Index(
                fields=["stage"],
            ),
            models.Index(
                fields=["created_at"],
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.phone})"

    @property
    def lead_id(self):
        return self.id

    def clean(self):
        super().clean()

        if self.organization_id and self.pipeline_id:
            if self.pipeline.organization_id != self.organization_id:
                raise ValidationError(
                    {
                        "pipeline": (
                            "Pipeline does not belong to this organization."
                        )
                    }
                )

        if self.pipeline_id and self.stage_id:
            if self.stage.pipeline_id != self.pipeline_id:
                raise ValidationError(
                    {
                        "stage": (
                            "Stage does not belong to this pipeline."
                        )
                    }
                )

        if self.phone:
            self.phone = normalize_phone(self.phone)

        duplicate_qs = Lead.objects.filter(
            organization_id=self.organization_id,
            phone=self.phone,
        )

        if self.pk:
            duplicate_qs = duplicate_qs.exclude(
                pk=self.pk,
            )

        if duplicate_qs.exists():
            raise ValidationError(
                {
                    "phone": (
                        "Duplicate lead created. "
                        "This phone number already exists "
                        "in this organization."
                    )
                }
            )