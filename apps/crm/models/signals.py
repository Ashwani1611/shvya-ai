from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.organizations.models import Organization

from .pipeline import Pipeline
from .stage import Stage


@receiver(post_save, sender=Organization)
def create_default_pipeline(sender, instance, created, **kwargs):
    """
    When a new Organization is created, give it a default pipeline
    and starting stage so the CRM isn't empty on day one.
    """
    if not created:
        return

    pipeline, _ = Pipeline.objects.get_or_create(
        organization=instance,
        name="Leads",
        defaults={
            "description": "Default SHVYA lead pipeline.",
            "is_active": True,
        },
    )

    Stage.objects.get_or_create(
        pipeline=pipeline,
        name="New Leads",
        defaults={
            "display_order": 1,
            "is_active": True,
        },
    )