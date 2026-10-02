"""Durable, privacy-preserving click receipts for tracked template CTAs."""

import uuid

from django.db import models


class WhatsAppTemplateCTAEvent(models.Model):
    """One confirmed user action from a SHVYA-tracked template button.

    Link previews and security scanners only load the confirmation page. An
    event is written after the recipient presses the action on that page, so
    analytics do not treat automated URL fetches as human clicks.
    """

    class ActionType(models.TextChoices):
        URL = "url", "Website"
        CALL = "call", "Call"
        COPY_CODE = "copy_code", "Copy code"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="whatsapp_template_cta_events",
    )
    account = models.ForeignKey(
        "channels.WhatsAppAccount",
        on_delete=models.CASCADE,
        related_name="template_cta_events",
    )
    template = models.ForeignKey(
        "channels.WhatsAppTemplate",
        on_delete=models.CASCADE,
        related_name="cta_events",
    )
    action_type = models.CharField(max_length=16, choices=ActionType.choices)
    button_index = models.PositiveSmallIntegerField()
    card_index = models.PositiveSmallIntegerField(null=True, blank=True)
    button_label = models.CharField(max_length=80)
    destination = models.TextField(blank=True)
    # Random browser identity is salted and hashed before storage. SHVYA never
    # stores the visitor cookie, IP address, or user-agent text in this table.
    visitor_hash = models.CharField(max_length=64)
    # One signed confirmation form may create at most one event, which makes
    # browser retries and double form submissions idempotent.
    request_key = models.CharField(max_length=64, unique=True)
    clicked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-clicked_at"]
        indexes = [
            models.Index(
                fields=["template", "clicked_at"],
                name="wa_cta_tpl_time_idx",
            ),
            models.Index(
                fields=["account", "clicked_at"],
                name="wa_cta_acct_time_idx",
            ),
            models.Index(
                fields=["organization", "clicked_at"],
                name="wa_cta_org_time_idx",
            ),
        ]

    def __str__(self):
        return f"{self.template_id} — {self.action_type} — {self.clicked_at}"
