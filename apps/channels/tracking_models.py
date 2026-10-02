"""Durable per-send WhatsApp template CTA links and click receipts."""

import uuid

from django.db import models

from apps.organizations.models import Organization


class WhatsAppTemplateTrackedLink(models.Model):
    """One unguessable tracked action for one outbound template button.

    A link is created before the provider send and activated only after Meta
    accepts the template message. The original destination/action stays on the
    server; the recipient sees only the random token appended to SHVYA's public
    tracking URL.
    """

    class ActionType(models.TextChoices):
        WEBSITE = "website", "Website"
        CALL = "call", "Call"
        COPY_CODE = "copy_code", "Copy code"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="whatsapp_template_tracked_links",
    )
    account = models.ForeignKey(
        "channels.WhatsAppAccount",
        on_delete=models.CASCADE,
        related_name="template_tracked_links",
    )
    template = models.ForeignKey(
        "channels.WhatsAppTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tracked_links",
    )
    message = models.ForeignKey(
        "channels.WhatsAppMessage",
        on_delete=models.CASCADE,
        related_name="template_tracked_links",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_template_tracked_links",
    )
    meta_template_id = models.CharField(max_length=128, blank=True, db_index=True)
    template_name = models.CharField(max_length=150, blank=True)
    button_path = models.CharField(max_length=64)
    button_index = models.PositiveSmallIntegerField(default=0)
    card_index = models.PositiveSmallIntegerField(null=True, blank=True)
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    button_text = models.CharField(max_length=80, blank=True)
    destination_url = models.URLField(max_length=2048, blank=True)
    phone_number = models.CharField(max_length=32, blank=True)
    coupon_code = models.CharField(max_length=128, blank=True)
    is_active = models.BooleanField(default=False, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["message", "button_path"],
                name="wa_cta_link_message_path_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["account", "sent_at"],
                name="wa_cta_link_acct_sent_idx",
            ),
            models.Index(
                fields=["meta_template_id", "sent_at"],
                name="wa_cta_link_tpl_sent_idx",
            ),
        ]


class WhatsAppTemplateTrackedClick(models.Model):
    """A real browser request for a tracked CTA token.

    Raw IP addresses are never stored. ``fingerprint`` is a keyed digest used
    only to suppress immediate browser retries and automated duplicate loads.
    """

    class EventType(models.TextChoices):
        CLICK = "click", "CTA click"
        ACTION = "action", "Action completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    link = models.ForeignKey(
        WhatsAppTemplateTrackedLink,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_type = models.CharField(
        max_length=12,
        choices=EventType.choices,
        default=EventType.CLICK,
    )
    fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    user_agent = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["link", "event_type", "created_at"],
                name="wa_cta_click_link_evt_idx",
            ),
        ]
