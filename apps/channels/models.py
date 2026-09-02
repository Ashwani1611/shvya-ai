import uuid

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

from apps.organizations.models import Organization

# ============================================================
# TOKEN ENCRYPTION
# ============================================================
#
# Meta access tokens must be stored reversibly (we need the raw
# value to call the Graph API), so they can't be hashed like
# APIKey.key_hash. They're encrypted at rest with Fernet, keyed
# off settings.SECRET_KEY.
#
# NOTE: rotating SECRET_KEY invalidates every stored token. If
# that becomes a problem, move this to its own dedicated
# encryption key read from .env instead of reusing SECRET_KEY.


def _fernet():
    key = settings.SECRET_KEY.encode("utf-8")
    # Fernet requires a 32-byte url-safe base64 key.
    import base64
    import hashlib

    digest = hashlib.sha256(key).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class EncryptedTextField(models.TextField):
    """
    Stores text encrypted at rest, transparent in Python.

    Not a general-purpose field yet -- if another app needs the
    same behavior, move this to core/fields.py.
    """

    def get_prep_value(self, value):
        if value is None or value == "":
            return value

        return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")

    def from_db_value(self, value, expression, connection):
        if not value:
            return value

        try:
            return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")

        except InvalidToken:
            # Value was stored before encryption was added, or the
            # encryption key changed. Fail loudly rather than silently
            # sending a garbled token to Meta's API.
            raise ValueError(
                "Stored WhatsApp credential could not be decrypted. "
                "SECRET_KEY may have changed since it was saved."
            )


# ============================================================
# WHATSAPP ACCOUNT
# ============================================================


class WhatsAppAccount(models.Model):
    """
    One organization's connected WhatsApp Business number.

    Two connection paths:

        API    -- the organization brings their own Meta System
                  User token + phone_number_id (self-managed
                  WhatsApp Business Platform app).

        coexisted -- SHVYA provisions and manages the number on the
                  organization's behalf (embedded signup or a
                  SHVYA-owned tech-provider WABA). Same fields are
                  populated, just sourced/rotated by SHVYA instead
                  of the org.

    Credentials come from Meta's WhatsApp Business Platform
    (Cloud API): a phone_number_id to send from, and a
    permanent/system-user access_token to authenticate with.
    """

    class ConnectionType(models.TextChoices):
        API = "api", "Connect API"
        coexisted = "hosted", "coexisted Account"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # NOTE: was OneToOneField -- changed to ForeignKey because an
    # organization can connect multiple WhatsApp numbers (e.g. one
    # per location/brand), matching the "Connect API" list view
    # where a single business has several rows.
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="whatsapp_accounts",
    )

    connection_type = models.CharField(
        max_length=10,
        choices=ConnectionType.choices,
        default=ConnectionType.API,
    )

    # Display name for this connection, shown as "Business Name"
    # in the Connect API table -- may differ from the org name if
    # the org runs multiple branded numbers.
    business_name = models.CharField(
        max_length=150,
        blank=True,
    )

    phone_number_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Meta WhatsApp phone_number_id used as the sender.",
    )

    waba_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Meta WhatsApp Business Account ID (for reference/lookup).",
    )

    display_phone_number = models.CharField(
        max_length=32,
        blank=True,
    )

    access_token = EncryptedTextField(
        blank=True,
        help_text="Meta system-user access token. Stored encrypted.",
    )

    welcome_message = models.TextField(
        blank=True,
        help_text="Auto-sent to a lead's first message on this number.",
    )

    request_contact_info = models.BooleanField(
        default=False,
        help_text="Ask the lead to share contact details before continuing.",
    )

    # ---------------------------------------------------------
    # Connection lifecycle
    # ---------------------------------------------------------
    #
    # coexisted accounts go through a provisioning step before
    # credentials exist; API accounts are considered connected
    # as soon as valid credentials are saved.

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONNECTED = "connected", "Connected"
        FAILED = "failed", "Failed"
        DISCONNECTED = "disconnected", "Disconnected"

    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.PENDING,
    )

    is_active = models.BooleanField(default=True)

    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-connected_at"]

    def __str__(self):
        return f"{self.organization.name} — {self.display_phone_number or self.phone_number_id or self.get_connection_type_display()}"


# ============================================================
# WHATSAPP MESSAGE
# ============================================================


class WhatsAppMessage(models.Model):
    """
    Log of every inbound and outbound WhatsApp message.

    external_id is Meta's message id (wamid). It's the
    idempotency key per CLAUDE.md rule 5 -- inbound webhook
    retries and outbound send retries must never create
    duplicate rows.
    """

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"
        RECEIVED = "received", "Received"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="whatsapp_messages",
    )

    account = models.ForeignKey(
        WhatsAppAccount,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    # Nullable: an inbound message may arrive before any Lead
    # exists for that phone number.
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_messages",
    )

    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
    )

    # Meta's wamid. Unique per message -- this is what makes
    # webhook retries and send retries idempotent.
    external_id = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
    )

    from_number = models.CharField(max_length=32)
    to_number = models.CharField(max_length=32)

    body = models.TextField(blank=True)

    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.QUEUED,
    )

    raw_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Full Meta payload for this message/status event.",
    )

    error = models.TextField(blank=True)

    # Only meaningful for inbound messages -- outbound messages are
    # created as "already read" (the agent just sent it). Powers
    # the unread badge in the Chats inbox.
    is_read = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["lead", "created_at"]),
        ]

    def __str__(self):
        return f"{self.direction}: {self.from_number} -> {self.to_number}"


# ============================================================
# BULK MESSAGING
# ============================================================


class BulkMessageCampaign(models.Model):
    """
    A one-off bulk send to a set of leads (targeted by pipeline
    and, optionally, stage). Each recipient becomes its own
    BulkMessageRecipient row, which in turn produces its own
    WhatsAppMessage once actually sent -- so a campaign is just
    an organizing/tracking wrapper, not a special send path.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="whatsapp_campaigns",
    )

    account = models.ForeignKey(
        WhatsAppAccount,
        on_delete=models.CASCADE,
        related_name="campaigns",
    )

    name = models.CharField(max_length=150)

    # Targeting: all leads in this pipeline, optionally narrowed
    # to one stage. Both are snapshotted at creation time via
    # BulkMessageRecipient rows, so later pipeline/stage changes
    # don't retroactively add/remove recipients mid-send.
    pipeline = models.ForeignKey(
        "crm.Pipeline",
        on_delete=models.CASCADE,
        related_name="whatsapp_campaigns",
    )

    stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_campaigns",
        help_text="Leave blank to target every stage in the pipeline.",
    )

    body = models.TextField(
        help_text=(
            "Free-form text. NOTE: Meta only allows free-form text to "
            "leads who messaged in the last 24 hours -- leads outside "
            "that window are skipped unless template_name is set."
        ),
    )

    template_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Meta-approved template name, for leads outside the 24h window.",
    )

    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="whatsapp_campaigns",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.organization.name})"


class BulkMessageRecipient(models.Model):
    """
    One lead's place in a BulkMessageCampaign. Snapshotting
    campaign -> recipients at creation time (rather than
    re-querying Lead by pipeline/stage at send time) means the
    campaign's audience is fixed once queued, and each recipient
    can be retried/tracked independently.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    campaign = models.ForeignKey(
        BulkMessageCampaign,
        on_delete=models.CASCADE,
        related_name="recipients",
    )

    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.CASCADE,
        related_name="whatsapp_bulk_recipients",
    )

    message = models.ForeignKey(
        WhatsAppMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bulk_recipient",
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )

    skip_reason = models.CharField(
        max_length=200,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "lead"],
                name="uniq_campaign_lead",
            )
        ]

    def __str__(self):
        return f"{self.campaign.name} -> {self.lead.name}"


# ============================================================
# MESSAGE TEMPLATES
# ============================================================
#
# Mirrors Meta's WhatsApp Business template system: a template
# must be submitted to Meta and approved before it can be used
# for template-based sends (outside the 24h free-form window, or
# for a lead's first message). SHVYA stores its own copy so
# templates can be listed/edited here, but `status` reflects
# Meta's actual approval state, not something SHVYA decides on
# its own -- see the "sync from Meta" note on WhatsAppTemplate.sync_status.


class WhatsAppTemplate(models.Model):

    class Category(models.TextChoices):
        MARKETING = "marketing", "Marketing"
        UTILITY = "utility", "Utility"
        AUTHENTICATION = "authentication", "Authentication"

    class Format(models.TextChoices):
        STANDARD = "standard", "Standard Template"
        CAROUSEL = "carousel", "Carousel Template"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PAUSED = "paused", "Paused"
        ARCHIVED = "archived", "Archived"
        PENDING_DELETION = "pending_deletion", "Pending Deletion"

    class AttachmentType(models.TextChoices):
        NONE = "none", "None"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        DOCUMENT = "document", "Document"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="whatsapp_templates",
    )

    # Which connected number/business this template is submitted
    # under -- Meta templates are approved per WABA, not globally,
    # matching the "Business" filter dropdown in the template list.
    account = models.ForeignKey(
        WhatsAppAccount,
        on_delete=models.CASCADE,
        related_name="templates",
    )

    name = models.CharField(max_length=150)

    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.MARKETING,
    )

    template_format = models.CharField(
        max_length=10,
        choices=Format.choices,
        default=Format.STANDARD,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    body = models.TextField(
        help_text="Use {{variable_name}} placeholders, e.g. {{lead_name}}.",
    )

    footer = models.CharField(
        max_length=60,
        blank=True,
    )

    attachment_type = models.CharField(
        max_length=10,
        choices=AttachmentType.choices,
        default=AttachmentType.NONE,
    )

    # Buttons: list of {"type": "visit_website"|"call_phone"|
    # "copy_offer"|"text_back"|"request_contact_info", ...config}
    buttons = models.JSONField(
        default=list,
        blank=True,
    )

    # Meta's own template id, once submitted -- null until then.
    meta_template_id = models.CharField(
        max_length=128,
        blank=True,
    )

    rejection_reason = models.TextField(blank=True)

    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="whatsapp_templates",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "name"],
                name="uniq_account_template_name",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.account})"