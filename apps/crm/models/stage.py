import uuid

from django.db import models

from .pipeline import Pipeline


class Stage(models.Model):

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

    def __str__(self):
        return f"{self.pipeline.name} → {self.name}"