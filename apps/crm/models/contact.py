import uuid

from django.db import models

from .lead import Lead


class LeadContact(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="contacts",
    )

    channel = models.CharField(max_length=30)
    handle = models.CharField(max_length=150)
    verified = models.BooleanField(default=False)

    metadata = models.JSONField(
        default=dict,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.channel}: {self.handle}"