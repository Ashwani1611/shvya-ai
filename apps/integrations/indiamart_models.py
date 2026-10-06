import uuid
from django.db import models


class IndiaMartConnection(models.Model):
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="indiamart_connection",
    )
    requested_at = models.DateTimeField(null=True, blank=True)
    webhook_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    pipeline = models.ForeignKey(
        "crm.Pipeline", on_delete=models.RESTRICT, null=True, blank=True
    )
    stage = models.ForeignKey(
        "crm.Stage", on_delete=models.RESTRICT, null=True, blank=True
    )
    is_enabled = models.BooleanField(default=False)
    generated_at = models.DateTimeField(null=True, blank=True)
    last_received_at = models.DateTimeField(null=True, blank=True)


class IndiaMartReceipt(models.Model):
    connection = models.ForeignKey(
        IndiaMartConnection, on_delete=models.CASCADE, related_name="receipts"
    )
    query_id = models.CharField(max_length=100)
    lead = models.ForeignKey("crm.Lead", on_delete=models.SET_NULL, null=True)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "query_id"], name="unique_indiamart_query"
            )
        ]
        ordering = ["-received_at"]
