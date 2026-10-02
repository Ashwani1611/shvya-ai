from django.db import models

from apps.organizations.models import Organization


class Document(models.Model):
    """
    Organization-owned knowledge source version.

    A Document represents one concrete version of a logical
    knowledge source.

    Examples:

        Website URL
            source_key = "https://example.com"
            version = 1

        Updated website
            source_key = "https://example.com"
            version = 2

        Uploaded pricing document
            source_key = "pricing.pdf"
            version = 1

    Only the active version of a source should participate in
    knowledge retrieval.
    """

    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    # ============================================================
    # OWNERSHIP
    # ============================================================

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="ai_documents",
    )

    # ============================================================
    # SOURCE IDENTITY
    # ============================================================

    name = models.CharField(
        max_length=255,
    )

    source_key = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text=(
            "Stable identifier for the logical knowledge source. "
            "For URLs this is normally the normalized URL. "
            "For uploaded files this can be the logical file/source name."
        ),
    )

    version = models.PositiveIntegerField(
        default=1,
    )

    # ============================================================
    # SOURCE CONTENT
    # ============================================================

    file = models.FileField(
        upload_to="ai_knowledge/%Y/%m/",
        blank=True,
    )

    source_url = models.URLField(
        blank=True,
    )

    share_instruction = models.TextField(
        blank=True,
        help_text="Tell the AI when and why this file should be sent to a lead.",
    )

    file_sharing_ready = models.BooleanField(
        default=False,
        help_text="Uploaded bytes passed file validation; independent of knowledge indexing.",
    )

    # ============================================================
    # PROCESSING
    # ============================================================

    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
    )

    processing_error = models.TextField(
        blank=True,
    )

    # ============================================================
    # STATUS
    # ============================================================

    is_active = models.BooleanField(
        default=True,
    )

    # ============================================================
    # TIMESTAMPS
    # ============================================================

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    # ============================================================
    # META
    # ============================================================

    class Meta:
        ordering = [
            "-updated_at",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "source_key",
                    "version",
                ],
                name="uniq_org_document_source_version",
            ),
        ]

        indexes = [
            models.Index(
                fields=[
                    "organization",
                    "source_key",
                    "is_active",
                ],
                name="ai_doc_source_active_idx",
            ),
        ]

    # ============================================================
    # STRING
    # ============================================================

    def __str__(self):
        return (
            f"{self.organization.name} - "
            f"{self.name} - "
            f"v{self.version}"
        )

    @property
    def failure_help(self):
        """Safe operator guidance; provider exception text may contain secrets."""
        error = (self.processing_error or "").casefold()
        if "credit" in error or "quota" in error or "billing" in error:
            return "Knowledge processing needs available AI credits or provider quota. Check the account balance, then retry."
        if "api_key" in error or "authentication" in error or "401" in error:
            return "The AI provider configuration needs attention. Ask an administrator to check it, then retry."
        if "ocr" in error or "readable text" in error:
            return "Text could not be read. Retry with the OCR-enabled worker; use a clear scan and split long scans into smaller PDFs."
        if "no such file" in error or "not found" in error or "permission" in error:
            return "The stored file could not be read. Ask an administrator to check shared media storage, or upload the file again."
        return "Processing did not finish. Retry the file; if it fails again, contact support with the filename."
