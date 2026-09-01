from django.db import models

from apps.organizations.models import Organization


class FAQ(models.Model):
    """
    Organization-owned frequently asked question.

    Each FAQ contains a customer-facing question and the
    corresponding answer SHVYA AI can use as knowledge.
    """

    # =========================================================
    # OWNERSHIP
    # =========================================================

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="ai_faqs",
    )

    # =========================================================
    # FAQ CONTENT
    # =========================================================

    question = models.TextField()

    answer = models.TextField()

    # =========================================================
    # STATUS
    # =========================================================

    is_active = models.BooleanField(
        default=True,
    )

    # =========================================================
    # TIMESTAMPS
    # =========================================================

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    # =========================================================
    # META
    # =========================================================

    class Meta:
        ordering = [
            "-updated_at",
        ]

        indexes = [
            models.Index(
                fields=[
                    "organization",
                    "is_active",
                ],
                name="ai_faq_org_active_idx",
            ),
        ]

    # =========================================================
    # STRING
    # =========================================================

    def __str__(self):
        return (
            f"{self.organization.name} - "
            f"{self.question[:80]}"
        )