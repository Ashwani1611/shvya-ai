"""
Bulk WhatsApp messaging -- create a campaign, snapshot its target
leads as BulkMessageRecipient rows, and hand off actual sending to
Celery tasks (apps.channels.tasks) so a large campaign never blocks
a request/response cycle.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.channels.models import (
    BulkMessageCampaign,
    BulkMessageRecipient,
    WhatsAppAccount,
)
from apps.crm.models import Lead


class BulkCampaignError(Exception):
    """Raised when a campaign can't be created or launched."""


def _resolve_target_leads(*, organization, pipeline, stage=None, tag=None):
    leads = Lead.objects.filter(
        organization=organization,
        pipeline=pipeline,
    )

    if stage:
        leads = leads.filter(stage=stage)

    if tag:
        leads = leads.filter(tags__tag=tag)

    return leads.distinct()


@transaction.atomic
def create_campaign(*, organization, created_by, name, pipeline, body, stage=None, tag=None, template_name=""):
    """
    Creates a BulkMessageCampaign in draft status and snapshots its
    target leads into BulkMessageRecipient rows. Does NOT send
    anything -- call launch_campaign() separately once the sender
    has confirmed the audience/body.
    """
    account = WhatsAppAccount.objects.filter(
        organization=organization,
        is_active=True,
        status=WhatsAppAccount.Status.CONNECTED,
    ).first()

    if not account:
        raise BulkCampaignError(
            "No connected WhatsApp account for this organization."
        )

    leads = _resolve_target_leads(
        organization=organization,
        pipeline=pipeline,
        stage=stage,
        tag=tag,
    )

    if not leads.exists():
        raise BulkCampaignError(
            "No leads match the selected audience."
        )

    campaign = BulkMessageCampaign.objects.create(
        organization=organization,
        account=account,
        name=name,
        pipeline=pipeline,
        stage=stage,
        body=body,
        template_name=template_name,
        created_by=created_by,
        status=BulkMessageCampaign.Status.DRAFT,
    )

    BulkMessageRecipient.objects.bulk_create(
        [
            BulkMessageRecipient(campaign=campaign, lead=lead)
            for lead in leads
        ]
    )

    return campaign


def launch_campaign(*, campaign):
    """
    Marks a draft campaign as queued and returns it. The caller
    (a view) is responsible for actually dispatching the Celery
    task -- kept separate so this function has no Celery
    dependency and stays easy to test.
    """
    if campaign.status != BulkMessageCampaign.Status.DRAFT:
        raise BulkCampaignError(
            f"Campaign is already {campaign.get_status_display()}."
        )

    campaign.status = BulkMessageCampaign.Status.QUEUED
    campaign.save(update_fields=["status"])

    return campaign


def is_within_24h_window(*, lead):
    """
    Meta only allows free-form text messages to a contact within
    24 hours of their last inbound message. Outside that window, a
    template message is required. Used to decide whether a
    recipient should be skipped (no template configured) or sent
    via template instead of free text.
    """
    from apps.channels.models import WhatsAppMessage

    last_inbound = (
        WhatsAppMessage.objects.filter(
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at")
        .first()
    )

    if not last_inbound:
        return False

    return (timezone.now() - last_inbound.created_at).total_seconds() < 24 * 3600


def mark_campaign_started(*, campaign):
    campaign.status = BulkMessageCampaign.Status.SENDING
    campaign.started_at = timezone.now()
    campaign.save(update_fields=["status", "started_at"])


def mark_campaign_completed(*, campaign):
    failed_count = campaign.recipients.filter(
        status=BulkMessageRecipient.Status.FAILED,
    ).count()

    total_count = campaign.recipients.count()

    campaign.status = (
        BulkMessageCampaign.Status.FAILED
        if failed_count == total_count and total_count > 0
        else BulkMessageCampaign.Status.COMPLETED
    )
    campaign.completed_at = timezone.now()
    campaign.save(update_fields=["status", "completed_at"])