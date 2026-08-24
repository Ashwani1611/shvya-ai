from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.organizations.models import Organization

from .pipeline import Pipeline
from .stage import Stage


# ============================================================
# DEFAULT PIPELINE STAGES
# ============================================================

DEFAULT_PIPELINE_STAGES = [
    {
        "name": "New leads",
        "display_order": 1,
    },
    {
        "name": "Qualified",
        "display_order": 2,
    },
    {
        "name": "Nurturing",
        "display_order": 3,
    },
    {
        "name": "Average lead",
        "display_order": 4,
    },
    {
        "name": "Ultra Hot",
        "display_order": 5,
    },
    {
        "name": "Lead Won",
        "display_order": 6,
    },
    {
        "name": "DNP",
        "display_order": 7,
    },
    {
        "name": "Lead Lost",
        "display_order": 8,
    },
]


# ============================================================
# DEFAULT PIPELINE FOR NEW ORGANIZATION
# ============================================================

@receiver(post_save, sender=Organization)
def create_default_pipeline(sender, instance, created, **kwargs):
    """
    When a new Organization is created, create its default
    "Leads" pipeline.

    The Pipeline post_save signal is responsible for creating
    the default stages.
    """

    if not created:
        return

    Pipeline.objects.get_or_create(
        organization=instance,
        name="Leads",
        defaults={
            "description": "Default SHVYA lead pipeline.",
            "is_active": True,
        },
    )


# ============================================================
# DEFAULT STAGES FOR EVERY NEW PIPELINE
# ============================================================

@receiver(post_save, sender=Pipeline)
def create_default_stages(sender, instance, created, **kwargs):
    """
    Whenever a new Pipeline is created, automatically create
    the standard SHVYA CRM stages.

    This applies to every organization and every pipeline.
    """

    if not created:
        return

    for stage_data in DEFAULT_PIPELINE_STAGES:

        Stage.objects.get_or_create(
            pipeline=instance,
            name=stage_data["name"],
            defaults={
                "display_order": stage_data["display_order"],
                "is_active": True,
            },
        )