import uuid

from django.db import models


class MarketingBookingRequest(models.Model):
    """Public request for a Shvya AI sales walkthrough."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey("crm.Lead", on_delete=models.PROTECT, related_name="call_requests")
    name = models.CharField(max_length=100)
    email = models.EmailField(max_length=200)
    phone = models.CharField(max_length=32)
    company = models.CharField(max_length=150, blank=True)
    preferred_date = models.DateField()
    preferred_time = models.CharField(max_length=16)
    goal = models.TextField(max_length=1200)
    interest = models.CharField(max_length=140, blank=True)
    consent = models.BooleanField(default=False)
    source_path = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, default="new")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "BAC request"
        verbose_name_plural = "BAC requests"

    def __str__(self):
        return f"{self.name} - {self.email}"
