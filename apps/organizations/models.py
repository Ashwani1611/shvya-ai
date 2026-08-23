import secrets
import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.db import models


class OrganizationTag(models.Model):
    """
    Platform-level tags for internal super-admin operations.

    Examples:
    - Discontinued
    - Trial
    - Whatsapp Ban
    - Growth Lab
    """

    name = models.CharField(
        max_length=50,
        unique=True,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


# ============================================================
# ORGANIZATION PAYMENT
# ============================================================


class OrganizationPayment(models.Model):
    """
    Individual payment received from an organization.

    Each organization can have multiple payment records.
    This allows Superadmin to maintain a complete payment
    history independently from the organization's overall
    contract/sale amount.
    """

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        BANK_TRANSFER = "bank_transfer", "Bank Transfer"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        OTHER = "other", "Other"

    organization = models.ForeignKey(
        "Organization",
        on_delete=models.CASCADE,
        related_name="payments",
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    payment_date = models.DateField()

    payment_method = models.CharField(
        max_length=30,
        choices=PaymentMethod.choices,
        default=PaymentMethod.OTHER,
    )

    reference_number = models.CharField(
        max_length=100,
        blank=True,
    )

    notes = models.TextField(
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "-payment_date",
            "-created_at",
        ]

        verbose_name = "Organization Payment"
        verbose_name_plural = "Organization Payments"

    def __str__(self):
        return (
            f"{self.organization.name} - "
            f"{self.amount} - "
            f"{self.payment_date}"
        )


# ============================================================
# ORGANIZATION
# ============================================================


class Organization(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    name = models.CharField(
        max_length=255,
    )

    timezone = models.CharField(
        max_length=64,
        default="Asia/Kolkata",
    )

    plan = models.CharField(
        max_length=32,
        default="free",
    )

    # ---------------------------------------------------------
    # Super Admin Package
    # ---------------------------------------------------------

    class Package(models.TextChoices):
        DIY = "diy", "DIY"
        DFY = "dfy", "DFY"
        FREE = "free", "Free"
        PRO = "pro", "Pro"

    class PaymentMode(models.TextChoices):
        FULL = "full", "Full Payment"
        PARTIAL = "partial", "Partial (Installments)"

    # ---------------------------------------------------------
    # Super Admin Account Management
    # ---------------------------------------------------------

    assigned_poc = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="poc_organizations",
        help_text="Internal point of contact for this account.",
    )

    # ---------------------------------------------------------
    # Credits
    # ---------------------------------------------------------

    credits_total = models.IntegerField(
        default=0,
    )

    credits_used = models.IntegerField(
        default=0,
    )

    credits_alert_enabled = models.BooleanField(
        default=False,
    )

    # ---------------------------------------------------------
    # Dates / Subscription
    # ---------------------------------------------------------

    renewal_payment_at = models.DateField(
        null=True,
        blank=True,
    )

    day_of_sale = models.DateField(
        null=True,
        blank=True,
    )

    onboarding_completion_date = models.DateField(
        null=True,
        blank=True,
    )

    disabled_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    # ---------------------------------------------------------
    # Seats / Package / Tags
    # ---------------------------------------------------------

    number_of_seats = models.PositiveIntegerField(
        default=1,
    )

    package = models.CharField(
        max_length=20,
        choices=Package.choices,
        default=Package.FREE,
    )

    tags = models.ManyToManyField(
        OrganizationTag,
        blank=True,
        related_name="organizations",
    )

    # ---------------------------------------------------------
    # Internal Super Admin Notes
    # ---------------------------------------------------------

    operational_notes = models.TextField(
        blank=True,
        help_text="Internal notes visible only to super admins.",
    )

    # ---------------------------------------------------------
    # Sale / Payment
    # ---------------------------------------------------------

    total_sale_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total contract/invoice amount for this organization.",
    )

    payment_mode = models.CharField(
        max_length=20,
        choices=PaymentMode.choices,
        default=PaymentMode.FULL,
    )

    # ---------------------------------------------------------
    # Existing Organization Status
    # ---------------------------------------------------------

    is_active = models.BooleanField(
        default=True,
    )

    settings = models.JSONField(
        default=dict,
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

    def __str__(self):
        return self.name

    # ---------------------------------------------------------
    # Super Admin Properties
    # ---------------------------------------------------------

    @property
    def credits_remaining(self):
        return max(
            self.credits_total - self.credits_used,
            0,
        )

    @property
    def next_payment_at(self):
        """
        Currently the next payment is represented by
        the organization's renewal date.
        """
        return self.renewal_payment_at


# ============================================================
# API KEY
# ============================================================


class APIKey(models.Model):
    """
    Organization-level API key used by external systems
    to call the SHVYA Lead Upsert API.

    The raw API key is NEVER stored.
    Only a secure hash is stored.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="api_keys",
    )

    name = models.CharField(
        max_length=100,
    )

    key_prefix = models.CharField(
        max_length=32,
        unique=True,
        editable=False,
    )

    key_hash = models.CharField(
        max_length=255,
        editable=False,
    )

    can_upsert_leads = models.BooleanField(
        default=True,
    )

    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["-created_at"]

        verbose_name = "Integration API Key"
        verbose_name_plural = "Integration API Keys"

    def __str__(self):
        return f"{self.name} ({self.organization.name})"

    @classmethod
    def issue(
        cls,
        organization,
        name,
        expires_at=None,
    ):
        """
        Generate a new API key.

        The raw key is returned only once.
        """

        raw_key = (
            f"shvya_{secrets.token_urlsafe(32)}"
        )

        key = cls.objects.create(
            organization=organization,
            name=name,
            key_prefix=raw_key[:16],
            key_hash=make_password(raw_key),
            expires_at=expires_at,
        )

        return key, raw_key

    def verify(self, raw_key):
        return check_password(
            raw_key,
            self.key_hash,
        )