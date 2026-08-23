import uuid

from django.conf import settings
from django.db import models

from .lead import Lead


class LeadNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="lead_notes",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    note = models.TextField()
    note_type = models.CharField(
        max_length=20,
        choices=[
            ("manual", "Manual"),
            ("system", "System"),
        ],
        default="manual",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Note on {self.lead.name} ({self.created_at:%Y-%m-%d})"