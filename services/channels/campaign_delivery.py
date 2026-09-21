"""Short database claims, real template transport, and durable attempt evidence."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.channels.campaign_models import CampaignAttempt, CampaignDelivery, CampaignPlan, CampaignSenderGate
from apps.channels.models import BulkMessageRecipient, WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError
from services.channels import whatsapp_service
from services.channels.whatsapp_error_service import describe_whatsapp_failure, failure_summary

from .campaign_audience import is_suppressed, rights, user_pipelines
from .campaign_policy import CampaignInputError
from .campaign_service import finish_campaign, retry_eligibility, validate_plan

logger = logging.getLogger(__name__)


def sync_legacy_recipient(delivery):
    if delivery.recipient_id:
        status = {"pending": "pending", "sending": "pending", "accepted": "sent", "failed": "failed", "skipped": "skipped", "review": "failed"}[delivery.state]
        BulkMessageRecipient.objects.filter(pk=delivery.recipient_id).update(status=status, skip_reason=delivery.error_message[:200])


def maybe_schedule_retry(delivery, plan):
    if plan.auto_retry:
        allowed, due, _ = retry_eligibility(delivery, plan, now=timezone.now())
        if allowed and not is_suppressed(organization_id=plan.campaign.organization_id, phone=delivery.phone, lead=delivery.lead):
            delivery.state, delivery.due_at, delivery.published_at = "pending", due, None
            plan.campaign.status, plan.campaign.completed_at = "queued", None
            plan.campaign.save(update_fields=["status", "completed_at"])


def _skip(delivery, reason):
    delivery.state, delivery.due_at, delivery.error_message = "skipped", None, reason
    delivery.save(update_fields=["state", "due_at", "error_message", "updated_at"])
    sync_legacy_recipient(delivery)


def send_delivery(delivery_id):
    campaign_id = CampaignDelivery.objects.filter(pk=delivery_id).values_list("campaign_id", flat=True).first()
    if campaign_id is None:
        return {"status": "missing"}
    with transaction.atomic():
        # Every mutator uses plan -> delivery -> attempt lock order. Cancellation
        # prevents a new claim; an HTTP request already in flight cannot be recalled.
        plan = CampaignPlan.objects.select_for_update(of=("self",)).select_related(
            "campaign__account", "consent_by__organization", "template__account",
        ).get(pk=campaign_id)
        delivery = CampaignDelivery.objects.select_for_update(of=("self",)).select_related("lead").get(pk=delivery_id)
        now = timezone.now()
        if delivery.state != "pending" or not plan.prepared_at or not delivery.due_at or delivery.due_at > now:
            return {"status": "not_due"}
        if plan.cancelled_at:
            _skip(delivery, "Campaign cancelled before sending.")
            return {"status": "cancelled"}
        if delivery.delivered_at or delivery.read_at or delivery.replied_at:
            delivery.state, delivery.due_at = "accepted", None
            delivery.save(update_fields=["state", "due_at", "updated_at"])
            sync_legacy_recipient(delivery)
            return {"status": "already_delivered_or_replied"}
        try:
            validate_plan(plan)
        except (CampaignInputError, PermissionDenied):
            _skip(delivery, "Account, template, or CRM permissions changed after review. Create a newly reviewed campaign.")
            return {"status": "configuration_changed"}
        if not delivery.lead_id or delivery.lead.phone != delivery.phone:
            _skip(delivery, "The original lead was deleted or its phone changed. No replacement recipient was used.")
            return {"status": "lead_changed"}
        current_pipeline = user_pipelines(plan.consent_by).filter(pk=delivery.lead.pipeline_id).first()
        if delivery.lead.organization_id != plan.campaign.organization_id or current_pipeline is None or not rights(plan.consent_by, current_pipeline)["can_edit_leads"]:
            _skip(delivery, "The recipient is no longer accessible to the campaign owner.")
            return {"status": "recipient_access_changed"}
        if is_suppressed(organization_id=plan.campaign.organization_id, phone=delivery.phone, lead=delivery.lead):
            _skip(delivery, "Recipient has opted out of campaign messages.")
            return {"status": "opted_out"}
        if not whatsapp_service.account_matches_lead_pipeline(
            account=plan.campaign.account,
            lead=delivery.lead,
        ):
            _skip(
                delivery,
                "The lead moved to a pipeline linked to another WhatsApp number.",
            )
            return {"status": "pipeline_whatsapp_changed"}
        if delivery.attempt_count >= 1 + plan.retry_attempts:
            _skip(delivery, "The campaign attempt limit has been reached.")
            return {"status": "attempt_limit"}
        CampaignSenderGate.objects.get_or_create(account_id=plan.campaign.account_id, defaults={"next_slot_at": now})
        gate = CampaignSenderGate.objects.select_for_update().get(account_id=plan.campaign.account_id)
        if gate.next_slot_at > now:
            delivery.due_at, delivery.published_at = gate.next_slot_at, now
            delivery.save(update_fields=["due_at", "published_at", "updated_at"])
            return {"status": "deferred", "due_at": delivery.due_at.isoformat()}
        gate.next_slot_at = now + timedelta(milliseconds=200)
        gate.save(update_fields=["next_slot_at"])
        message = whatsapp_service.queue_outbound_message(
            organization=plan.campaign.organization, account=plan.campaign.account, lead=delivery.lead,
            to_number=delivery.phone, body=delivery.body, message_type=WhatsAppMessage.MessageType.TEXT,
            media_payload={"transport": "template", "template_id": str(plan.template_id),
                           "template_name": plan.template_snapshot["name"], "language_code": plan.template_snapshot["language"],
                           "components": delivery.components},
        )
        message.status = "sending"
        message.save(update_fields=["status", "updated_at"])
        delivery.attempt_count += 1
        attempt = CampaignAttempt.objects.create(delivery=delivery, number=delivery.attempt_count, message=message)
        delivery.state, delivery.claim_id, delivery.claimed_at, delivery.due_at = "sending", uuid.uuid4(), now, None
        delivery.error_code, delivery.error_message, delivery.http_status, delivery.uncertain = "", "", None, False
        delivery.save()
        if delivery.recipient_id:
            BulkMessageRecipient.objects.filter(pk=delivery.recipient_id).update(message=message, status="pending", skip_reason="")
        campaign = plan.campaign
        campaign.status, campaign.completed_at = "sending", None
        if campaign.started_at is None:
            campaign.started_at = now
        campaign.save(update_fields=["status", "started_at", "completed_at"])
        attempt_id = attempt.pk

    # Absolutely no database row locks span the provider HTTP request.
    failure, uncertain, http_status = None, False, None
    try:
        whatsapp_service.send_outbound_message(message=message)
        message.refresh_from_db()
        if not message.external_id:
            uncertain = True
            failure = {"code": "SHVYA_UNCONFIRMED", "why": "The provider returned no message ID. Delivery cannot be confirmed.", "resolve": "Check the connected WhatsApp account before sending again."}
    except Exception as exc:
        message.refresh_from_db()
        cause = exc.__cause__
        http_status = cause.status_code if isinstance(cause, WhatsAppAPIError) else None
        uncertain = http_status is None
        if uncertain:
            failure = {"code": "SHVYA_UNCONFIRMED", "why": "The send outcome is uncertain; the provider may already have accepted it.", "resolve": "Reconcile the original message. Automatic and bulk retries are blocked to prevent duplicates."}
        else:
            failure = describe_whatsapp_failure(raw_payload=message.raw_payload, error_text=message.error)
        logger.warning("Campaign attempt %s did not return confirmed acceptance (HTTP %s)", attempt_id, http_status)

    with transaction.atomic():
        plan = CampaignPlan.objects.select_for_update(of=("self",)).select_related("campaign").get(pk=campaign_id)
        delivery = CampaignDelivery.objects.select_for_update(of=("self",)).select_related("lead").get(pk=delivery_id)
        attempt = CampaignAttempt.objects.select_for_update().get(pk=attempt_id)
        now = timezone.now()
        attempt.finished_at = now
        if message.external_id:
            attempt.provider_id = message.external_id
        if failure is None:
            attempt.accepted_at = attempt.accepted_at or now
            delivery.accepted_at = delivery.accepted_at or now
            # A failed webhook can beat the HTTP response. Preserve it rather
            # than incorrectly turning a known provider rejection into success.
            if delivery.attempt_count == attempt.number and not attempt.failed_at and delivery.state == "sending":
                delivery.state = "accepted"
        else:
            attempt.uncertain, attempt.http_status = uncertain, http_status
            attempt.error_code = str(failure.get("code") or "")[:64]
            attempt.error_message = failure_summary(failure)[:2000]
            if not uncertain:
                attempt.failed_at = attempt.failed_at or now
            if delivery.attempt_count == attempt.number and not (delivery.delivered_at or delivery.read_at or delivery.replied_at):
                # If a webhook already proved acceptance/delivery, an HTTP
                # timeout must not replace that evidence with a review state.
                if not (uncertain and (attempt.sent_at or attempt.delivered_at or attempt.read_at)):
                    delivery.state = "review" if uncertain else "failed"
                    delivery.failed_at = None if uncertain else now
                    delivery.error_code, delivery.error_message = attempt.error_code, attempt.error_message
                    delivery.http_status, delivery.uncertain = http_status, uncertain
                    maybe_schedule_retry(delivery, plan)
        attempt.save()
        delivery.save()
        sync_legacy_recipient(delivery)
    finish_campaign(campaign_id)
    return {"status": delivery.state, "attempt_id": str(attempt_id)}


def recover_expired_claims():
    """A worker lost after claiming may have sent. Never automatically replay it."""
    stale = list(CampaignDelivery.objects.filter(state="sending", claimed_at__lt=timezone.now() - timedelta(minutes=10)).values_list("pk", "campaign_id")[:200])
    for delivery_id, campaign_id in stale:
        with transaction.atomic():
            CampaignPlan.objects.select_for_update().get(pk=campaign_id)
            delivery = CampaignDelivery.objects.select_for_update().get(pk=delivery_id)
            if delivery.state != "sending" or delivery.delivered_at or delivery.read_at:
                continue
            delivery.state, delivery.uncertain = "review", True
            delivery.error_code = "SHVYA_WORKER_INTERRUPTED"
            delivery.error_message = "A worker stopped after claiming this recipient. Check delivery; it will not be resent automatically."
            delivery.save(update_fields=["state", "uncertain", "error_code", "error_message", "updated_at"])
            CampaignAttempt.objects.filter(delivery=delivery, number=delivery.attempt_count, finished_at__isnull=True).update(uncertain=True, error_code=delivery.error_code, error_message=delivery.error_message)
            sync_legacy_recipient(delivery)
        finish_campaign(campaign_id)
    return len(stale)
