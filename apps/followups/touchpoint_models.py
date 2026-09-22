"""Shared, tenant-owned saved replies for Cadence and every inbox."""

import uuid

from django.core.validators import MaxLengthValidator
from django.db import models


class TouchpointCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=100)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="touchpoint_org_category_uniq"
            )
        ]


class TouchpointReply(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(
        TouchpointCategory, on_delete=models.CASCADE, related_name="replies"
    )
    title = models.CharField(max_length=150)
    body = models.TextField(max_length=1000, validators=[MaxLengthValidator(1000)])
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "id"]
