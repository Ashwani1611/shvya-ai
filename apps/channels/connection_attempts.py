"""Audit model for WhatsApp API connection attempts.

This model intentionally stores only safe diagnostics. OAuth authorization codes
and access-token values are never persisted here. Successful credentials live on
WhatsAppAccount, where the access token is encrypted at rest.
"""

import uuid

from django.db import models

from apps.organizations.models import Organization


class WhatsAppConnectionAttempt(models.Model):
    """One manual or Meta Embedded Signup connection attempt."""

    class Method(models.TextChoices):
        EMBEDDED = "embedded", "Meta Embedded Signup"
        MANUAL = "manual", "Manual Access Token"

    class Status(models.TextChoices):
        STARTED = "started", "Started"
        META_FINISHED = "meta_finished", "Meta Finished"
        CODE_RECEIVED = "code_received", "OAuth Code Received"
        CALLBACK_RECEIVED = "callback_received", "Backend Callback Received"
        TOKEN_EXCHANGED = "token_exchanged", "Token Exchanged"
        PHONE_VERIFIED = "phone_verified", "Phone Verified"
        CONNECTED = "connected", "Connected"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="whatsapp_connection_attempts",
    )

    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_connection_attempts",
    )

    account = models.ForeignKey(
        "channels.WhatsAppAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="connection_attempts",
    )

    method = models.CharField(
        max_length=16,
        choices=Method.choices,
    )

    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.STARTED,
    )

    # Human-readable machine stage used to pinpoint exactly where a flow stopped.
    # Examples: started, meta_finish, oauth_code_missing, token_exchange,
    # phone_lookup, account_saved, webhook_subscription.
    stage = models.CharField(
        max_length=64,
        blank=True,
    )

    waba_id = models.CharField(
        max_length=64,
        blank=True,
    )

    phone_number_id = models.CharField(
        max_length=64,
        blank=True,
    )

    display_phone_number = models.CharField(
        max_length=32,
        blank=True,
    )

    business_name = models.CharField(
        max_length=150,
        blank=True,
    )

    # Booleans only. Never store the OAuth code or access-token value here.
    code_received = models.BooleanField(
        default=False,
    )

    token_received = models.BooleanField(
        default=False,
    )

    # Null means the subscription step was not reached yet.
    webhook_subscribed = models.BooleanField(
        null=True,
        blank=True,
    )

    meta_error_code = models.CharField(
        max_length=64,
        blank=True,
    )

    error_message = models.TextField(
        blank=True,
    )

    warning_message = models.TextField(
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "created_at"],
                name="wa_attempt_org_created_idx",
            ),
            models.Index(
                fields=["organization", "status"],
                name="wa_attempt_org_status_idx",
            ),
        ]

    def __str__(self):
        return f"{self.get_method_display()} — {self.get_status_display()} — {self.organization}"
