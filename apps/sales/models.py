import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from apps.organizations.models import Organization


class DocumentType(models.TextChoices):
    QUOTATION = "quotation", "Quotation"
    AGREEMENT = "agreement", "Agreement"
    INVOICE = "invoice", "Invoice"


class SalesTemplate(models.Model):
    """Organization-owned design and communication defaults for one document type."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_templates",
    )
    document_type = models.CharField(max_length=16, choices=DocumentType.choices)
    name = models.CharField(max_length=120)
    number_prefix = models.CharField(max_length=12, blank=True)

    logo_url = models.URLField(max_length=2048, blank=True)
    logo_file = models.FileField(
        upload_to="sales/template-assets/logos/",
        blank=True,
    )
    signature_url = models.URLField(max_length=2048, blank=True)
    signature_file = models.FileField(
        upload_to="sales/template-assets/signatures/",
        blank=True,
    )
    accent_color = models.CharField(
        max_length=7,
        default="#0071e3",
        validators=[
            RegexValidator(
                regex=r"^#[0-9A-Fa-f]{6}$",
                message="Use a six-digit hex color such as #0071e3.",
            )
        ],
    )
    header_text = models.CharField(max_length=255, blank=True)
    body_template = models.TextField()
    footer_text = models.TextField(blank=True)

    email_subject_template = models.CharField(max_length=255, blank=True)
    email_body_template = models.TextField(blank=True)
    whatsapp_body_template = models.TextField(blank=True)

    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_templates_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["document_type", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type", "name"],
                name="sales_template_org_type_name_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "document_type", "is_active"],
                name="sales_tpl_org_type_active",
            )
        ]

    def __str__(self):
        return f"{self.organization.name} - {self.name}"


class SalesDocumentNumberSequence(models.Model):
    """Concurrency-safe sequence for organization-scoped sales document numbers."""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_number_sequences",
    )
    document_type = models.CharField(max_length=16, choices=DocumentType.choices)
    next_number = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type"],
                name="sales_sequence_org_type_uniq",
            )
        ]


class SalesDocument(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        SIGNED = "signed", "Signed"
        UNPAID = "unpaid", "Unpaid"
        PARTIAL = "partial", "Partially paid"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_documents",
    )
    document_type = models.CharField(max_length=16, choices=DocumentType.choices)
    template = models.ForeignKey(
        SalesTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    source_document = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="derived_documents",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_documents",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_documents_created",
    )

    document_number = models.CharField(max_length=64)
    title = models.CharField(max_length=180)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    recipient_name = models.CharField(max_length=180, blank=True)
    recipient_email = models.EmailField(blank=True)
    recipient_phone = models.CharField(max_length=40, blank=True)

    currency = models.CharField(max_length=8, default="INR")
    issue_date = models.DateField()
    valid_until = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    line_items = models.JSONField(default=list, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    content = models.TextField(blank=True)
    terms = models.TextField(blank=True)

    presentation_snapshot = models.JSONField(default=dict, blank=True)
    rendered_html = models.TextField(blank=True)
    email_subject_snapshot = models.CharField(max_length=255, blank=True)
    email_body_snapshot = models.TextField(blank=True)
    whatsapp_body_snapshot = models.TextField(blank=True)

    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    accepted_by_name = models.CharField(max_length=180, blank=True)
    accepted_by_email = models.EmailField(blank=True)
    accepted_ip = models.GenericIPAddressField(null=True, blank=True)

    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type", "document_number"],
                name="sales_doc_org_type_number_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "document_type", "status", "created_at"],
                name="sales_doc_org_type_status",
            ),
            models.Index(
                fields=["organization", "lead", "created_at"],
                name="sales_doc_org_lead_created",
            ),
        ]

    def __str__(self):
        return f"{self.document_number} - {self.title}"


class SalesDocumentDelivery(models.Model):
    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sales_document_deliveries",
    )
    document = models.ForeignKey(
        SalesDocument,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    channel = models.CharField(max_length=12, choices=Channel.choices)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
    )

    from_identity = models.CharField(max_length=255, blank=True)
    to_identity = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)

    provider_message_id = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)

    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_deliveries_sent",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "channel", "status", "created_at"],
                name="sales_delivery_org_channel",
            )
        ]

    def __str__(self):
        return f"{self.document.document_number} via {self.channel}"
