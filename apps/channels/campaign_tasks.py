"""Campaign work stays on the existing general Celery worker, not AI realtime."""
import logging
from datetime import datetime, timedelta

from celery import shared_task
from django.db.models import Min, Q
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="campaigns.prepare", ignore_result=True)
def prepare_campaign_task(campaign_id):
    from services.channels.campaign_service import prepare_campaign_chunk

    # One bounded transaction per task; the scheduler recovers a lost wake-up.
    if prepare_campaign_chunk(campaign_id):
        prepare_campaign_task.delay(str(campaign_id))


@shared_task(name="campaigns.send_recipient", ignore_result=True, rate_limit="10/s")
def send_campaign_recipient_task(delivery_id):
    from apps.channels.campaign_models import CampaignDelivery
    from services.channels.campaign_delivery import send_delivery

    result = send_delivery(delivery_id)
    if result.get("status") == "deferred":
        try:
            send_campaign_recipient_task.apply_async(args=[str(delivery_id)], eta=datetime.fromisoformat(result["due_at"]))
        except Exception:
            CampaignDelivery.objects.filter(pk=delivery_id, state="pending").update(published_at=None)
            logger.exception("Campaign deferred-send publication failed for %s", delivery_id)
    return result


@shared_task(name="campaigns.dispatch", ignore_result=True)
def dispatch_campaigns_task():
    from apps.channels.campaign_models import CampaignDelivery, CampaignPlan
    from services.channels.campaign_events import reconcile_events
    from services.channels.campaign_service import finish_campaign

    _, changed_campaigns = reconcile_events()
    for campaign_id in changed_campaigns:
        finish_campaign(campaign_id)
    # Preparing is a durable state. Duplicate tasks are harmless because chunks
    # lock the plan and advance the cursor in the same transaction as CRM writes.
    plans = CampaignPlan.objects.filter(prepared_at__isnull=True, cancelled_at__isnull=True, prepare_error="")
    for campaign_id in plans.values_list("pk", flat=True)[:10]:
        prepare_campaign_task.delay(str(campaign_id))
    now = timezone.now()
    available = Q(published_at__isnull=True) | Q(published_at__lt=now - timedelta(seconds=90))
    due = CampaignDelivery.objects.filter(
        available, state="pending", due_at__lte=now,
        campaign__campaign_plan__prepared_at__isnull=False, campaign__campaign_plan__cancelled_at__isnull=True,
        campaign__campaign_plan__prepare_error="",
    )
    accounts = list(due.values("campaign__account_id").annotate(first_due=Min("due_at")).order_by("first_due")[:20])
    published = 0
    for account in accounts:
        for offset, delivery_id in enumerate(due.filter(campaign__account_id=account["campaign__account_id"]).order_by("due_at", "pk").values_list("pk", flat=True)[:5]):
            if not CampaignDelivery.objects.filter(available, pk=delivery_id, state="pending", due_at__lte=now).update(published_at=now):
                continue
            try:
                send_campaign_recipient_task.apply_async(args=[str(delivery_id)], countdown=offset * 0.21)
                published += 1
            except Exception:
                CampaignDelivery.objects.filter(pk=delivery_id, state="pending").update(published_at=None)
                logger.exception("Campaign send publication failed for %s", delivery_id)
    # Covers campaigns made entirely of skipped recipients and early-return
    # worker paths; historical webhook changes can still update their metrics.
    for campaign_id in CampaignPlan.objects.filter(prepared_at__isnull=False, campaign__status__in=["queued", "sending"]).values_list("pk", flat=True)[:100]:
        finish_campaign(campaign_id)
    return published


@shared_task(name="campaigns.maintain", ignore_result=True)
def maintain_campaigns_task():
    from apps.channels.campaign_models import CampaignEvent, CampaignUpload
    from services.channels.campaign_delivery import recover_expired_claims

    recover_expired_claims()
    now = timezone.now()
    CampaignEvent.objects.filter(applied_at__isnull=True, received_at__lt=now - timedelta(days=7)).delete()
    CampaignEvent.objects.filter(applied_at__lt=now - timedelta(days=90)).delete()
    CampaignUpload.objects.filter(plan__isnull=True, expires_at__lt=now).delete()
    CampaignUpload.objects.filter(Q(plan__cancelled_at__isnull=False) | ~Q(plan__prepare_error=""), expires_at__lt=now).update(rows=[], reviewed_rows=[])


def register_campaign_schedule(sender, **kwargs):
    """Use the existing Beat/general worker; no deployment configuration changes."""
    sender.add_periodic_task(10.0, dispatch_campaigns_task.s(), name="bulk-campaign-dispatch")
    sender.add_periodic_task(60.0, maintain_campaigns_task.s(), name="bulk-campaign-maintenance")


from celery import current_app  # noqa: E402

current_app.on_after_finalize.connect(register_campaign_schedule, weak=False)
if current_app.finalized:
    register_campaign_schedule(current_app)
