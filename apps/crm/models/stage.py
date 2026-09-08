import uuid

from django.core.exceptions import ValidationError
from django.db import models

from .pipeline import Pipeline


class Stage(models.Model):

    PROTECTED_STAGE_NAMES = frozenset({"new lead", "qualified"})

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    pipeline = models.ForeignKey(
        Pipeline,
        on_delete=models.CASCADE,
        related_name="stages",
    )

    name = models.CharField(
        max_length=100,
    )

    description = models.TextField(
        blank=True,
        help_text=(
            "Describe what this stage means and what should happen "
            "while a lead is in this stage."
        ),
    )

    display_order = models.PositiveIntegerField(
        default=0,
    )

    color = models.CharField(
        max_length=20,
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    ai_on = models.BooleanField(
        default=True,
        help_text=(
            "Allow SHVYA AI to engage leads while they are in this stage."
        ),
    )

    config = models.JSONField(
        default=dict,
        blank=True,
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
            "name",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "pipeline",
                    "name",
                ],
                condition=models.Q(
                    is_active=True,
                ),
                name="uniq_active_pipeline_stage_name",
            ),

            models.UniqueConstraint(
                fields=[
                    "pipeline",
                    "display_order",
                ],
                name="uniq_pipeline_display_order",
            ),
        ]

    @staticmethod
    def _normalized_name(value):
        return str(value or "").strip().casefold()

    @property
    def is_name_locked(self):
        return self._normalized_name(self.name) in self.PROTECTED_STAGE_NAMES

    def save(self, *args, **kwargs):
        if self.pk:
            original_name = (
                Stage.objects.filter(pk=self.pk)
                .values_list("name", flat=True)
                .first()
            )
            if (
                original_name
                and self._normalized_name(original_name) in self.PROTECTED_STAGE_NAMES
                and self._normalized_name(self.name) != self._normalized_name(original_name)
            ):
                raise ValidationError(
                    {"name": f"{original_name} is a system stage and cannot be renamed."}
                )
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.pipeline.name} → {self.name}"
