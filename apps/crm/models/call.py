import uuid

from django.conf import settings
from django.db import models

from .lead import Lead


class LeadCall(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="calls",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ("completed", "Completed"),
            ("no_response", "No Response"),
            ("busy", "Busy"),
            ("scheduled", "Scheduled"),
            ("cancelled", "Cancelled"),
        ],
    )

    call_name = models.CharField(
        max_length=150,
        blank=True,
    )

    duration_seconds = models.PositiveIntegerField(
        default=0
    )

    notes = models.TextField(
        blank=True
    )
    called_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-called_at"]

    def __str__(self):
        return f"Call to {self.lead.name} ({self.status})"