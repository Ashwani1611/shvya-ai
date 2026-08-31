import uuid

from django.db import models

from apps.organizations.models import Organization


class AnalyticsSettings(models.Model):
    """
    Org-level configuration that powers the Analytics dashboard.

    Stage semantics (which Stage counts as "hot", "won", "lost")
    aren't stored anywhere on Stage itself -- Stage is just a named,
    ordered column in a pipeline, with no is_won/is_lost flag. Rather
    than guess from stage names (fragile -- "Won", "Closed Won", and
    "Deal Closed" are all plausible and none are guaranteed), the org
    explicitly maps specific Stage rows to each role here.

    One settings row per organization. Stages are picked from across
    all of the org's pipelines (the dashboard UI groups the dropdown
    options by pipeline name so the choice is unambiguous).
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="analytics_settings",
    )

    hot_lead_stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    lead_won_stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    lead_lost_stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Number of days a lead can sit in its current stage with no
    # activity before it's considered "stalled". Used by future
    # stall-detection widgets -- not yet wired into a metric below,
    # but stored now so the settings form has somewhere to save it.
    stall_day_threshold = models.PositiveIntegerField(
        default=7,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        app_label = "analytics"
        verbose_name = "Analytics Settings"
        verbose_name_plural = "Analytics Settings"

    def __str__(self):
        return f"Analytics settings — {self.organization.name}"

    @property
    def is_configured(self):
        return bool(
            self.hot_lead_stage_id
            and self.lead_won_stage_id
            and self.lead_lost_stage_id
        )