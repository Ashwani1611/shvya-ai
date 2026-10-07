"""Shared, tenant-owned saved replies for Cadence and every inbox."""

import uuid

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from apps.support.storage import private_storage

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


def touchpoint_attachment_upload_to(instance, filename):
    """Randomized tenant path; encrypted storage blocks unauthenticated media access."""
    return f"touchpoints/{instance.reply.category.organization_id}/{uuid.uuid4().hex}"


class TouchpointAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reply = models.ForeignKey(
        TouchpointReply, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(
        storage=private_storage, upload_to=touchpoint_attachment_upload_to,
        max_length=300,
    )
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=120)
    size = models.PositiveBigIntegerField()
    position = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["reply", "position"], name="touchpoint_attachment_position_uniq"
            ),
        ]


@receiver(post_delete, sender=TouchpointAttachment)
def delete_touchpoint_file(sender, instance, **kwargs):
    if instance.file and instance.file.name:
        storage, name = instance.file.storage, instance.file.name
        transaction.on_commit(lambda: storage.delete(name))
