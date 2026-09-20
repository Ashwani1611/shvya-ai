import uuid

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.db import models

from apps.core.crypto import credential_cipher
from apps.organizations.models import Organization


def _sales_cipher():
    return credential_cipher(purpose="shvya-sales-gateways-v1")


class SalesEmailTrackedLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(
        "sales.SalesDocumentDelivery",
        on_delete=models.CASCADE,
        related_name="tracked_links",
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    target_url = models.URLField(max_length=4096)
    click_count = models.PositiveIntegerField(default=0)
    first_clicked_at = models.DateTimeField(null=True, blank=True)
    last_clicked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]


class SalesActivity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_activities",
    )
    document = models.ForeignKey(
        "sales.SalesDocument",
        on_delete=models.CASCADE,
        related_name="activities",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_activity_events",
    )
    event_type = models.CharField(max_length=64)
    message = models.CharField(max_length=500)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "document", "created_at"],
                name="sales_activity_doc_created",
            ),
            models.Index(
                fields=["organization", "event_type", "created_at"],
                name="sales_activity_type_created",
            ),
        ]


class SalesAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_attachments",
    )
    document = models.ForeignKey(
        "sales.SalesDocument",
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="sales/attachments/%Y/%m/%d/", max_length=400)
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=120)
    size = models.PositiveBigIntegerField(default=0)
    visible_to_customer = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_attachments_uploaded",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "document", "created_at"],
                name="sales_attachment_doc_created",
            )
        ]


class SalesScheduledDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        PARTIAL = "partial", "Partially completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_scheduled_deliveries",
    )
    document = models.ForeignKey(
        "sales.SalesDocument",
        on_delete=models.CASCADE,
        related_name="scheduled_deliveries",
    )
    channels = models.JSONField(default=list)
    email_subject = models.CharField(max_length=255, blank=True)
    email_body = models.TextField(blank=True)
    whatsapp_body = models.TextField(blank=True)
    whatsapp_template = models.ForeignKey(
        "channels.WhatsAppTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_scheduled_deliveries",
    )
    base_url = models.URLField(max_length=2048, blank=True)
    scheduled_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error_message = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_schedules_created",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scheduled_at", "created_at"]
        indexes = [
            models.Index(
                fields=["status", "scheduled_at"],
                name="sales_schedule_due_idx",
            ),
            models.Index(
                fields=["organization", "status", "scheduled_at"],
                name="sales_schedule_org_due",
            ),
        ]


class SalesReminder(models.Model):
    class Kind(models.TextChoices):
        QUOTATION_EXPIRY = "quotation_expiry", "Quotation expiry"
        AGREEMENT_EXPIRY = "agreement_expiry", "Agreement expiry"
        INVOICE_DUE = "invoice_due", "Invoice due"
        INVOICE_OVERDUE = "invoice_overdue", "Invoice overdue"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_reminders",
    )
    document = models.ForeignKey(
        "sales.SalesDocument",
        on_delete=models.CASCADE,
        related_name="reminders",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    due_on = models.DateField()
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "kind", "due_on"],
                name="sales_reminder_doc_kind_due_uniq",
            )
        ]


class SalesSettings(models.Model):
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_settings",
    )
    quotation_expiry_reminder_days = models.PositiveSmallIntegerField(default=3)
    agreement_expiry_reminder_days = models.PositiveSmallIntegerField(default=7)
    invoice_due_reminder_days = models.PositiveSmallIntegerField(default=3)
    invoice_overdue_repeat_days = models.PositiveSmallIntegerField(default=3)
    automatic_email_reminders = models.BooleanField(default=True)
    attach_customer_files_to_email = models.BooleanField(default=False)

    quotation_reminder_subject = models.CharField(
        max_length=255,
        default="Quotation {{document.number}} expires soon",
    )
    quotation_reminder_body = models.TextField(
        default=(
            "Hi {{recipient.name}},\n\n"
            "Quotation {{document.number}} expires on {{document.valid_until}}.\n\n"
            "Review it here: {{document.url}}"
        )
    )
    agreement_reminder_subject = models.CharField(
        max_length=255,
        default="Agreement {{document.number}} expires soon",
    )
    agreement_reminder_body = models.TextField(
        default=(
            "Hi {{recipient.name}},\n\n"
            "Agreement {{document.number}} reaches its end/review date on "
            "{{document.valid_until}}.\n\nView it here: {{document.url}}"
        )
    )
    invoice_due_subject = models.CharField(
        max_length=255,
        default="Invoice {{document.number}} is due soon",
    )
    invoice_due_body = models.TextField(
        default=(
            "Hi {{recipient.name}},\n\n"
            "Invoice {{document.number}} is due on {{document.due_date}}.\n\n"
            "View invoice: {{document.url}}"
        )
    )
    invoice_overdue_subject = models.CharField(
        max_length=255,
        default="Invoice {{document.number}} is overdue",
    )
    invoice_overdue_body = models.TextField(
        default=(
            "Hi {{recipient.name}},\n\n"
            "Invoice {{document.number}} is overdue by {{invoice.days_overdue}} day(s).\n\n"
            "View invoice: {{document.url}}"
        )
    )
    updated_at = models.DateTimeField(auto_now=True)


class SalesPaymentGateway(models.Model):
    class Provider(models.TextChoices):
        RAZORPAY = "razorpay", "Razorpay"
        STRIPE = "stripe", "Stripe"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_payment_gateways",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    display_name = models.CharField(max_length=120, blank=True)
    public_key = models.CharField(max_length=255, blank=True)
    encrypted_secret = models.TextField(blank=True)
    encrypted_webhook_secret = models.TextField(blank=True)
    is_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "provider"],
                name="sales_gateway_org_provider_uniq",
            )
        ]

    def set_secret(self, value):
        value = str(value or "")
        self.encrypted_secret = (
            _sales_cipher().encrypt(value.encode("utf-8")).decode("ascii")
            if value
            else ""
        )

    def get_secret(self):
        if not self.encrypted_secret:
            return ""
        try:
            return _sales_cipher().decrypt(
                self.encrypted_secret.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""

    def set_webhook_secret(self, value):
        value = str(value or "")
        self.encrypted_webhook_secret = (
            _sales_cipher().encrypt(value.encode("utf-8")).decode("ascii")
            if value
            else ""
        )

    def get_webhook_secret(self):
        if not self.encrypted_webhook_secret:
            return ""
        try:
            return _sales_cipher().decrypt(
                self.encrypted_webhook_secret.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""


class SalesPaymentCheckout(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PAID = "paid", "Paid"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_payment_checkouts",
    )
    invoice = models.ForeignKey(
        "sales.SalesDocument",
        on_delete=models.CASCADE,
        related_name="payment_checkouts",
    )
    gateway = models.ForeignKey(
        SalesPaymentGateway,
        on_delete=models.PROTECT,
        related_name="checkouts",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=8)
    provider_reference = models.CharField(max_length=255, blank=True)
    external_payment_id = models.CharField(max_length=255, blank=True)
    checkout_url = models.URLField(max_length=4096, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.CREATED,
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_checkouts_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "invoice", "status"],
                name="sales_checkout_invoice_idx",
            )
        ]


class SalesPayment(models.Model):
    class Kind(models.TextChoices):
        PAYMENT = "payment", "Payment"
        REFUND = "refund", "Refund"

    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        PENDING = "pending", "Pending"
        FAILED = "failed", "Failed"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        GATEWAY = "gateway", "Payment gateway"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_payments",
    )
    invoice = models.ForeignKey(
        "sales.SalesDocument",
        on_delete=models.CASCADE,
        related_name="payments",
    )
    checkout = models.ForeignKey(
        SalesPaymentCheckout,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PAYMENT)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.SUCCEEDED,
    )
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.OTHER)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=8)
    payment_date = models.DateField()
    reference_number = models.CharField(max_length=160, blank=True)
    external_payment_id = models.CharField(max_length=255, blank=True)
    provider = models.CharField(max_length=20, blank=True)
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_payments_recorded",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-payment_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "provider", "external_payment_id", "kind"],
                condition=~models.Q(external_payment_id=""),
                name="sales_payment_provider_external_uniq",
            )
        ]


class SalesCreditNote(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPLIED = "applied", "Applied"
        REFUNDED = "refunded", "Refunded"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_credit_notes",
    )
    invoice = models.ForeignKey(
        "sales.SalesDocument",
        on_delete=models.CASCADE,
        related_name="credit_notes",
    )
    credit_number = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.TextField(blank=True)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_credit_notes_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "credit_number"],
                name="sales_credit_org_number_uniq",
            )
        ]


class SalesRecurringInvoice(models.Model):
    class IntervalUnit(models.TextChoices):
        DAY = "day", "Day"
        WEEK = "week", "Week"
        MONTH = "month", "Month"
        YEAR = "year", "Year"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_recurring_invoices",
    )
    source_invoice = models.OneToOneField(
        "sales.SalesDocument",
        on_delete=models.CASCADE,
        related_name="recurring_rule",
    )
    interval_count = models.PositiveSmallIntegerField(default=1)
    interval_unit = models.CharField(max_length=8, choices=IntervalUnit.choices)
    next_run_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    max_cycles = models.PositiveIntegerField(null=True, blank=True)
    cycles_created = models.PositiveIntegerField(default=0)
    auto_send_email = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_recurring_rules_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["is_active", "next_run_at"],
                name="sales_recurring_due_idx",
            )
        ]
