import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .pipeline import Pipeline


class PipelinePermission(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    pipeline = models.ForeignKey(
        Pipeline,
        on_delete=models.CASCADE,
        related_name="permissions",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pipeline_permissions",
    )

    can_view_pipeline = models.BooleanField(
        default=True,
    )

    can_create_leads = models.BooleanField(
        default=False,
    )

    can_edit_leads = models.BooleanField(
        default=False,
    )

    can_move_leads = models.BooleanField(
        default=False,
    )

    can_delete_leads = models.BooleanField(
        default=False,
    )

    can_manage_stages = models.BooleanField(
        default=False,
    )

    can_manage_pipeline = models.BooleanField(
        default=False,
    )

    can_manage_lead_fields = models.BooleanField(
        default=False,
    )

    can_manage_users = models.BooleanField(
        default=False,
    )

    can_manage_api_keys = models.BooleanField(
        default=False,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "pipeline",
                    "user",
                ],
                name="uniq_pipeline_user_permission",
            )
        ]

    def clean(self):
        super().clean()

        if self.user.organization_id != self.pipeline.organization_id:
            raise ValidationError(
                "User and pipeline must belong to the same organization."
            )

        if self.user.is_superuser:
            raise ValidationError(
                "Platform superadmin does not need pipeline permissions."
            )

    def __str__(self):
        return f"{self.user.email} → {self.pipeline.name}"