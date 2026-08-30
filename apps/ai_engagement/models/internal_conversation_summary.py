import uuid

from django.conf import settings
from django.db import models

from apps.crm.models import Lead
from apps.organizations.models import Organization


class InternalConversationSummary(models.Model):
    """
    AI-generated internal summary of a Lead's conversation.

    This is intentionally separate from:

        - Qualification Summary
        - Lead Notes
        - Customer-facing messages

    The summary is intended for internal CRM users and is
    displayed from the Lead Card through a dedicated summary
    action/icon.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # ============================================================
    # TENANT
    # ============================================================

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="internal_conversation_summaries",
    )

    # ============================================================
    # LEAD
    # ============================================================

    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="internal_conversation_summaries",
    )

    # ============================================================
    # SUMMARY
    # ============================================================

    summary = models.TextField()

    # ============================================================
    # SOURCE SNAPSHOT
    # ============================================================

    source_message_count = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Number of conversation messages considered when "
            "this summary was generated."
        ),
    )

    # ============================================================
    # GENERATION METADATA
    # ============================================================

    generated_by = models.CharField(
        max_length=100,
        default="shvya_ai",
        help_text=(
            "Identifier for the component/provider that generated "
            "the summary."
        ),
    )

    model_name = models.CharField(
        max_length=150,
        blank=True,
        help_text=(
            "AI model used to generate this summary."
        ),
    )

    # ============================================================
    # ACTIVE VERSION
    # ============================================================

    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Whether this is the currently published summary "
            "for the lead."
        ),
    )

    # ============================================================
    # OPTIONAL USER/WORKER TRACE
    # ============================================================

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_internal_conversation_summaries",
    )

    # ============================================================
    # TIMESTAMPS
    # ============================================================

    generated_at = models.DateTimeField(
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
            "-generated_at",
        ]

        indexes = [
            models.Index(
                fields=[
                    "organization",
                    "lead",
                    "is_active",
                ],
                name="ai_ics_org_lead_active_idx",
            ),
            models.Index(
                fields=[
                    "lead",
                    "-generated_at",
                ],
                name="ai_ics_lead_generated_idx",
            ),
        ]

    # ============================================================
    # STRING
    # ============================================================

    def __str__(self):
        return (
            f"Conversation Summary - "
            f"{self.lead.name}"
        )