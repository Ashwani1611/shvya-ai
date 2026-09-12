import base64
import hashlib
import uuid

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.organizations.models import Organization


def _integration_fernet():
    """Return a stable Fernet instance derived from Django's SECRET_KEY."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class WebhookConfiguration(models.Model):
    """Organization-scoped outbound lead webhook configuration."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="webhook_configuration",
    )
    endpoint_url = models.URLField(
        max_length=2048,
        blank=True,
    )
    encrypted_secret = models.TextField(
        blank=True,
    )
    is_enabled = models.BooleanField(
        default=False,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["organization__name"]
        verbose_name = "Webhook Configuration"
        verbose_name_plural = "Webhook Configurations"

    def __str__(self):
        return f"Webhook - {self.organization.name}"

    @property
    def has_secret(self):
        return bool(self.encrypted_secret)

    def set_secret(self, raw_secret):
        raw_secret = str(raw_secret or "")
        self.encrypted_secret = (
            _integration_fernet().encrypt(raw_secret.encode("utf-8")).decode("ascii")
            if raw_secret
            else ""
        )

    def get_secret(self):
        if not self.encrypted_secret:
            return ""

        try:
            return _integration_fernet().decrypt(
                self.encrypted_secret.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""


class WebhookDelivery(models.Model):
    """Immutable payload plus delivery state for one outbound lead event."""

    class EventType(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RETRYING = "retrying", "Retrying"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    webhook = models.ForeignKey(
        WebhookConfiguration,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="webhook_deliveries",
    )
    lead_id = models.UUIDField()
    event_type = models.CharField(
        max_length=10,
        choices=EventType.choices,
    )
    payload = models.JSONField(
        default=dict,
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    attempt_count = models.PositiveSmallIntegerField(
        default=0,
    )
    response_status = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )
    response_body = models.TextField(
        blank=True,
    )
    error_message = models.TextField(
        blank=True,
    )
    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "status", "created_at"],
                name="webhook_org_status_created",
            ),
            models.Index(
                fields=["lead_id", "created_at"],
                name="webhook_lead_created",
            ),
        ]
        verbose_name = "Webhook Delivery"
        verbose_name_plural = "Webhook Deliveries"

    def __str__(self):
        return f"{self.event_type} {self.lead_id} ({self.status})"


class EmailConfiguration(models.Model):
    """Single organization-scoped SMTP identity used by email automations."""

    class Provider(models.TextChoices):
        GMAIL = "gmail", "Gmail / Google Workspace"
        MICROSOFT = "microsoft", "Microsoft 365 / Outlook"
        ZOHO = "zoho", "Zoho Mail"
        CUSTOM = "custom", "Custom SMTP"

    class Security(models.TextChoices):
        STARTTLS = "starttls", "STARTTLS"
        SSL = "ssl", "SSL/TLS"
        NONE = "none", "None"

    class TestStatus(models.TextChoices):
        NOT_TESTED = "not_tested", "Not tested"
        SUCCESS = "success", "Connected"
        FAILED = "failed", "Connection failed"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="email_configuration",
    )
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.CUSTOM,
    )
    email_address = models.EmailField(
        max_length=254,
    )
    sender_name = models.CharField(
        max_length=120,
        blank=True,
    )
    reply_to_email = models.EmailField(
        max_length=254,
        blank=True,
    )
    smtp_host = models.CharField(
        max_length=255,
    )
    smtp_port = models.PositiveIntegerField(
        default=587,
    )
    smtp_security = models.CharField(
        max_length=16,
        choices=Security.choices,
        default=Security.STARTTLS,
    )
    smtp_username = models.CharField(
        max_length=254,
    )
    encrypted_password = models.TextField(
        blank=True,
    )
    is_enabled = models.BooleanField(
        default=False,
    )
    last_test_status = models.CharField(
        max_length=16,
        choices=TestStatus.choices,
        default=TestStatus.NOT_TESTED,
    )
    last_tested_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    last_error = models.TextField(
        blank=True,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["organization__name"]
        verbose_name = "Email Configuration"
        verbose_name_plural = "Email Configurations"

    def __str__(self):
        return f"Email - {self.organization.name} ({self.email_address})"

    @property
    def has_password(self):
        return bool(self.encrypted_password)

    @property
    def is_connected(self):
        return (
            self.is_enabled
            and self.last_test_status == self.TestStatus.SUCCESS
            and self.has_password
        )

    def set_password(self, raw_password):
        raw_password = str(raw_password or "")
        self.encrypted_password = (
            _integration_fernet()
            .encrypt(raw_password.encode("utf-8"))
            .decode("ascii")
            if raw_password
            else ""
        )

    def get_password(self):
        if not self.encrypted_password:
            return ""

        try:
            return _integration_fernet().decrypt(
                self.encrypted_password.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""


class GoogleSheetIntegration(models.Model):
    """One organization-scoped Google worksheet -> CRM lead sync configuration."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="google_sheet_integrations",
    )
    name = models.CharField(
        max_length=120,
        default="Google Sheets Leads",
    )
    spreadsheet_id = models.CharField(
        max_length=255,
        blank=True,
    )
    spreadsheet_url = models.URLField(
        max_length=2048,
        blank=True,
    )
    sheet_id = models.CharField(
        max_length=64,
        blank=True,
    )
    worksheet_name = models.CharField(
        max_length=180,
        default="Sheet1",
    )
    pipeline = models.ForeignKey(
        "crm.Pipeline",
        on_delete=models.CASCADE,
        related_name="google_sheet_integrations",
    )
    stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.CASCADE,
        related_name="google_sheet_integrations",
    )
    mapping = models.JSONField(
        default=dict,
        blank=True,
    )
    discovered_headers = models.JSONField(
        default=list,
        blank=True,
    )
    webhook_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    encrypted_secret = models.TextField(
        blank=True,
    )
    import_existing = models.BooleanField(
        default=True,
    )
    is_enabled = models.BooleanField(
        default=False,
    )
    last_registered_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    last_synced_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    last_error = models.TextField(
        blank=True,
    )
    created_count = models.PositiveBigIntegerField(
        default=0,
    )
    updated_count = models.PositiveBigIntegerField(
        default=0,
    )
    skipped_count = models.PositiveBigIntegerField(
        default=0,
    )
    error_count = models.PositiveBigIntegerField(
        default=0,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "is_enabled", "created_at"],
                name="gsheet_org_enabled_created",
            ),
        ]
        verbose_name = "Google Sheets Integration"
        verbose_name_plural = "Google Sheets Integrations"

    def __str__(self):
        return f"{self.organization.name} - {self.name} ({self.worksheet_name})"

    @property
    def has_secret(self):
        return bool(self.encrypted_secret)

    def set_secret(self, raw_secret):
        raw_secret = str(raw_secret or "")
        self.encrypted_secret = (
            _integration_fernet().encrypt(raw_secret.encode("utf-8")).decode("ascii")
            if raw_secret
            else ""
        )

    def get_secret(self):
        if not self.encrypted_secret:
            return ""
        try:
            return _integration_fernet().decrypt(
                self.encrypted_secret.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""

    def clean(self):
        super().clean()
        if self.organization_id and self.pipeline_id:
            if self.pipeline.organization_id != self.organization_id:
                raise ValidationError(
                    {"pipeline": "Pipeline does not belong to this organization."}
                )
        if self.pipeline_id and self.stage_id:
            if self.stage.pipeline_id != self.pipeline_id:
                raise ValidationError(
                    {"stage": "Stage does not belong to the selected pipeline."}
                )


class MetaLeadPage(models.Model):
    """Organization-scoped Facebook Page used for Meta Lead Ads."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="meta_lead_pages"
    )
    page_id = models.CharField(max_length=80)
    page_name = models.CharField(max_length=150, blank=True)
    encrypted_page_access_token = models.TextField()
    encrypted_app_secret = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "page_id"], name="uniq_meta_page_org"
            )
        ]

    def set_page_access_token(self, value):
        self.encrypted_page_access_token = _integration_fernet().encrypt(
            str(value).encode("utf-8")
        ).decode("ascii")

    def get_page_access_token(self):
        try:
            return _integration_fernet().decrypt(
                self.encrypted_page_access_token.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""

    def set_app_secret(self, value):
        self.encrypted_app_secret = (
            _integration_fernet().encrypt(str(value).encode("utf-8")).decode("ascii")
            if value else ""
        )

    def get_app_secret(self):
        if not self.encrypted_app_secret:
            return ""
        try:
            return _integration_fernet().decrypt(
                self.encrypted_app_secret.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""

    def __str__(self):
        return self.page_name or self.page_id


class MetaLeadForm(models.Model):
    """Routing and field mapping for one Meta instant form."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(
        MetaLeadPage, on_delete=models.CASCADE, related_name="forms"
    )
    form_id = models.CharField(max_length=100)
    form_name = models.CharField(max_length=200)
    pipeline = models.ForeignKey(
        "crm.Pipeline", on_delete=models.PROTECT, related_name="meta_lead_forms"
    )
    stage = models.ForeignKey(
        "crm.Stage", on_delete=models.PROTECT, related_name="meta_lead_forms"
    )
    field_mapping = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["form_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["page", "form_id"], name="uniq_meta_form_page"
            )
        ]

    def clean(self):
        super().clean()
        if self.pipeline_id and self.stage_id:
            if self.stage.pipeline_id != self.pipeline_id:
                raise ValidationError(
                    {"stage": "Stage must belong to the selected pipeline."}
                )
        if self.page_id and self.pipeline_id:
            if self.pipeline.organization_id != self.page.organization_id:
                raise ValidationError(
                    {"pipeline": "Pipeline must belong to this organization."}
                )

    def __str__(self):
        return self.form_name
