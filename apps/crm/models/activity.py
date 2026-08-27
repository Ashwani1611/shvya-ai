import uuid

from django.conf import settings
from django.db import models

from .lead import Lead
from .pipeline import Pipeline
from .stage import Stage


class LeadActivity(models.Model):
    """
    Permanent chronological activity/history record for a Lead.

    This model is intentionally independent from the Lead's current
    pipeline/stage state.

    Important:
        - Activities belong to the Lead.
        - Moving a Lead between stages/pipelines does not remove history.
        - Pipeline/stage names are snapshotted so historical entries remain
          understandable even if the configuration is renamed later.
        - Actor identity is also snapshotted.
    """

    class Topic(models.TextChoices):
        LEAD_CREATED = (
            "lead_created",
            "Lead Created",
        )
        LEAD_UPDATED = (
            "lead_updated",
            "Lead Updated",
        )
        PIPELINE_CHANGED = (
            "pipeline_changed",
            "Pipeline Changed",
        )
        STAGE_CHANGED = (
            "stage_changed",
            "Stage Changed",
        )
        NOTE_ADDED = (
            "note_added",
            "Note Added",
        )
        CALL_LOGGED = (
            "call_logged",
            "Call Logged",
        )
        REMINDER_CREATED = (
            "reminder_created",
            "Reminder Created",
        )
        REMINDER_COMPLETED = (
            "reminder_completed",
            "Reminder Completed",
        )

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # ------------------------------------------------------------
    # LEAD
    # ------------------------------------------------------------

    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name="activities",
    )

    # ------------------------------------------------------------
    # TENANT
    #
    # Keep organization explicit so activity queries can always be
    # tenant-scoped without relying only on nested relations.
    # ------------------------------------------------------------

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="lead_activities",
    )

    # ------------------------------------------------------------
    # TOPIC
    # ------------------------------------------------------------

    topic = models.CharField(
        max_length=50,
        choices=Topic.choices,
    )

    # ------------------------------------------------------------
    # ACTOR
    #
    # Keep the relationship when the user still exists, but also
    # preserve the historical display name independently.
    # ------------------------------------------------------------

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lead_activities",
    )

    actor_name = models.CharField(
        max_length=255,
        blank=True,
    )

    # ------------------------------------------------------------
    # PIPELINE SNAPSHOT
    # ------------------------------------------------------------

    old_pipeline = models.ForeignKey(
        Pipeline,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    old_pipeline_name = models.CharField(
        max_length=255,
        blank=True,
    )

    new_pipeline = models.ForeignKey(
        Pipeline,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    new_pipeline_name = models.CharField(
        max_length=255,
        blank=True,
    )

    # ------------------------------------------------------------
    # STAGE SNAPSHOT
    # ------------------------------------------------------------

    old_stage = models.ForeignKey(
        Stage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    old_stage_name = models.CharField(
        max_length=255,
        blank=True,
    )

    new_stage = models.ForeignKey(
        Stage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    new_stage_name = models.CharField(
        max_length=255,
        blank=True,
    )

    # ------------------------------------------------------------
    # DETAILS
    #
    # Flexible structured metadata for event-specific information.
    #
    # Examples:
    #   Call status/duration
    #   Reminder title/due time
    #   Note preview
    #   Lead field changes
    # ------------------------------------------------------------

    details = models.JSONField(
        default=dict,
        blank=True,
    )

    # ------------------------------------------------------------
    # TIMESTAMP
    # ------------------------------------------------------------

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = [
            "-created_at",
        ]

        indexes = [
            models.Index(
                fields=[
                    "organization",
                    "lead",
                    "-created_at",
                ],
            ),
            models.Index(
                fields=[
                    "lead",
                    "-created_at",
                ],
            ),
            models.Index(
                fields=[
                    "topic",
                    "-created_at",
                ],
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_topic_display()} — "
            f"{self.lead.name}"
        )