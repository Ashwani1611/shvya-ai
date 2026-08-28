import re
import uuid

from django.core.exceptions import ValidationError
from django.db import models

from apps.organizations.models import Organization


class AttributeDefinition(models.Model):
    """
    Organization-level definition of a custom Lead attribute.

    The actual Lead-specific value remains stored in Lead.attributes.
    This model stores the metadata describing that attribute.
    """

    class FieldType(models.TextChoices):
        TEXT = "text", "Text"
        NUMERIC = "numeric", "Numeric"
        DATE = "date", "Date"
        DATETIME = "datetime", "Datetime"
        OPTION = "option", "Option Picker"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="crm_attribute_definitions",
    )

    name = models.CharField(
        max_length=100,
    )

    key = models.CharField(
        max_length=100,
    )

    field_type = models.CharField(
        max_length=20,
        choices=FieldType.choices,
        default=FieldType.TEXT,
    )

    description = models.TextField(
        blank=True,
    )

    options = models.JSONField(
        default=list,
        blank=True,
    )

    display_order = models.PositiveIntegerField(
        default=0,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "display_order",
            "created_at",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "name",
                ],
                name="uniq_org_attribute_name",
            ),
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "key",
                ],
                name="uniq_org_attribute_key",
            ),
        ]

        indexes = [
            models.Index(
                fields=[
                    "organization",
                    "display_order",
                ],
            ),
        ]

    def clean(self):
        super().clean()

        if self.name:
            self.name = self.name.strip()

        if self.key:
            self.key = self.key.strip().lower()

        if not self.name:
            raise ValidationError(
                {
                    "name": "Attribute name is required.",
                }
            )

        if not self.key:
            raise ValidationError(
                {
                    "key": "Attribute key is required.",
                }
            )

        if not re.fullmatch(
            r"[a-z0-9_]+",
            self.key,
        ):
            raise ValidationError(
                {
                    "key": (
                        "Attribute key may contain only "
                        "lowercase letters, numbers, and underscores."
                    ),
                }
            )

        if self.field_type == self.FieldType.OPTION:

            if not isinstance(
                self.options,
                list,
            ):
                raise ValidationError(
                    {
                        "options": (
                            "Options must be stored as a list."
                        ),
                    }
                )

            cleaned_options = []

            for option in self.options:

                value = str(option).strip()

                if not value:
                    continue

                if value not in cleaned_options:
                    cleaned_options.append(value)

            if not cleaned_options:
                raise ValidationError(
                    {
                        "options": (
                            "Option Picker must have at least "
                            "one option."
                        ),
                    }
                )

            self.options = cleaned_options

        else:
            self.options = []

    def __str__(self):
        return self.name