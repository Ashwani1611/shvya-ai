"""Persisted Instagram connection, conversation, message, and webhook state."""

import uuid

from django.conf import settings
from django.db import models

from apps.organizations.models import Organization

from .models import EncryptedTextField


class InstagramAccount(models.Model):
    """One Instagram professional account connected to one SHVYA workspace."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONNECTED = "connected", "Connected"
        EXPIRED = "expired", "Token Expired"
        REVOKED = "revoked", "Access Revoked"
        ERROR = "error", "Error"
        DISCONNECTED = "disconnected", "Disconnected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="instagram_account",
    )
    ig_user_id = models.CharField(max_length=80, unique=True)
    username = models.CharField(max_length=150, blank=True)
    display_name = models.CharField(max_length=200, blank=True)
    account_type = models.CharField(max_length=40, blank=True)
    profile_picture_url = models.URLField(max_length=1000, blank=True)
    access_token = EncryptedTextField(blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    token_refreshed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    webhook_subscribed = models.BooleanField(default=False)
    subscribed_fields = models.JSONField(default=list, blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="instagram_connections",
    )
    connected_at = models.DateTimeField(null=True, blank=True)
    last_webhook_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["organization", "status"], name="ig_acct_org_status_idx"),
            models.Index(fields=["token_expires_at"], name="ig_acct_token_exp_idx"),
        ]

    def __str__(self):
        return f"@{self.username or self.ig_user_id} — {self.organization}"


class InstagramOAuthAttempt(models.Model):
    """Short-lived queued OAuth completion state; raw code stays encrypted."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        CONNECTED = "connected", "Connected"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="instagram_oauth_attempts",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="instagram_oauth_attempts",
    )
    authorization_code = EncryptedTextField(blank=True)
    redirect_uri = models.URLField(max_length=1000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    error_message = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"], name="ig_oauth_org_created_idx"),
        ]

    def __str__(self):
        return f"Instagram OAuth {self.status} — {self.organization}"


class InstagramConversation(models.Model):
    """Local read model for an Instagram one-to-one messaging conversation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="instagram_conversations",
    )
    account = models.ForeignKey(
        InstagramAccount,
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    meta_conversation_id = models.CharField(max_length=160, null=True, blank=True, unique=True)
    participant_id = models.CharField(max_length=160)
    participant_username = models.CharField(max_length=150, blank=True)
    participant_name = models.CharField(max_length=200, blank=True)
    participant_profile_picture_url = models.URLField(max_length=1000, blank=True)
    last_message_text = models.TextField(blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_direction = models.CharField(max_length=10, blank=True)
    unread_count = models.PositiveIntegerField(default=0)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_message_at", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "participant_id"],
                name="uniq_ig_account_participant",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "last_message_at"], name="ig_conv_org_last_idx"),
        ]

    def __str__(self):
        return f"{self.account} ↔ {self.participant_username or self.participant_id}"


class InstagramMessage(models.Model):
    """Idempotent persisted inbound/outbound Instagram message."""

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        RECEIVED = "received", "Received"
        READ = "read", "Read"
        FAILED = "failed", "Failed"

    class MessageType(models.TextChoices):
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        AUDIO = "audio", "Audio"
        VIDEO = "video", "Video"
        SHARE = "share", "Share"
        STICKER = "sticker", "Sticker"
        POSTBACK = "postback", "Postback"
        UNKNOWN = "unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="instagram_messages",
    )
    account = models.ForeignKey(
        InstagramAccount,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    conversation = models.ForeignKey(
        InstagramConversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    external_id = models.CharField(max_length=300, null=True, blank=True, unique=True)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    status = models.CharField(max_length=12, choices=Status.choices)
    message_type = models.CharField(
        max_length=16,
        choices=MessageType.choices,
        default=MessageType.TEXT,
    )
    sender_id = models.CharField(max_length=160, blank=True)
    recipient_id = models.CharField(max_length=160, blank=True)
    body = models.TextField(blank=True)
    attachments = models.JSONField(default=list, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    is_read = models.BooleanField(default=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["organization", "created_at"], name="ig_msg_org_created_idx"),
            models.Index(fields=["conversation", "created_at"], name="ig_msg_conv_created_idx"),
        ]

    def __str__(self):
        return f"Instagram {self.direction} — {self.external_id or self.id}"


class InstagramWebhookDelivery(models.Model):
    """Durable idempotency envelope for signed Meta webhook deliveries."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payload_sha256 = models.CharField(max_length=64, unique=True)
    raw_payload = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["status", "received_at"], name="ig_hook_status_recv_idx")]

    def __str__(self):
        return f"Instagram webhook {self.payload_sha256[:12]} — {self.status}"
