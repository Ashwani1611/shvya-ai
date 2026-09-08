"""AI engagement signal hooks.

WhatsApp inbound AI engagement is intentionally queued by the channel service
after the inbound transaction commits. Do not also queue inbound messages from
a ``WhatsAppMessage`` post-save signal because that would duplicate replies.

This module only reacts to control-state changes that must affect already
queued automation work.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.ai_engagement.models import OrgInfo
from apps.hosted_automation.models import HostedAutomationJob


@receiver(post_save, sender=OrgInfo)
def skip_queued_hosted_ai_when_org_ai_disabled(sender, instance, **kwargs):
    """Do not let disabled organization AI block a due Hosted sequence.

    A Hosted AI job may already be waiting in the one-minute delay window when
    the organization master switch is turned off. Mark queued AI jobs skipped
    immediately so the sequence dispatcher can continue. Processing jobs are
    left alone because the AI task performs its own permission re-check before
    finalizing or sending a reply.
    """

    if instance.ai_enabled:
        return

    now = timezone.now()
    HostedAutomationJob.objects.filter(
        organization=instance.organization,
        status=HostedAutomationJob.Status.QUEUED,
    ).update(
        status=HostedAutomationJob.Status.SKIPPED,
        completed_at=now,
        result={"reason": "organization_ai_disabled"},
        updated_at=now,
    )
