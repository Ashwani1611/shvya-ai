from django.db import models

from apps.organizations.models import Organization


class OrgInfo(models.Model):
    """
    Organization-level AI configuration.

    This stores the core information and global AI controls
    used by SHVYA AI.
    """

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="org_info",
    )

    # =========================================================
    # ORGANIZATION AI CONTEXT
    # =========================================================

    about = models.TextField(
        blank=True,
        help_text=(
            "Describe what the organization does. "
            "This information is provided to SHVYA AI as context."
        ),
    )

    bot_languages = models.CharField(
        max_length=500,
        blank=True,
        help_text=(
            "Languages SHVYA AI can use when communicating "
            "with leads. Example: English, Hindi, Hinglish."
        ),
    )

    qualification_requirements = models.TextField(
        blank=True,
        help_text=(
            "Instructions and requirements SHVYA AI should use "
            "when qualifying leads."
        ),
    )

    # =========================================================
    # GLOBAL AI CONTROL
    # =========================================================

    ai_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Master switch controlling whether SHVYA AI "
            "can operate for this organization."
        ),
    )

    # =========================================================
    # GLOBAL BUMP-UP CONTROL
    # =========================================================

    bump_up_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Allow SHVYA AI to send bump-up messages "
            "to leads who have not responded."
        ),
    )

    bump_up_count = models.PositiveIntegerField(
        default=2,
        help_text=(
            "Maximum number of bump-up messages that may "
            "be sent to a lead."
        ),
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

    class Meta:
        ordering = [
            "-updated_at",
        ]

        verbose_name = "Organization AI Info"
        verbose_name_plural = "Organization AI Info"

    def __str__(self):
        return f"AI Info - {self.organization.name}"