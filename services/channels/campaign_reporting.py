"""Database-derived campaign reporting; unknown legacy evidence stays unknown."""
from django.core.exceptions import ValidationError
from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone

from apps.channels.models import WhatsAppAccount
from services.crm.lead_chat import pipeline_chat_account

from .campaign_audience import require_manage, user_pipelines, uuid_value
from .campaign_policy import CampaignInputError, percent
from .campaign_service import retry_eligibility


def iso(value):
    return value.isoformat() if value else None


def _campaign_chat_url(lead):
    """Route recipient actions through the number linked to the lead's current pipeline."""
    if lead is None:
        return None
    try:
        account = pipeline_chat_account(lead)
    except ValidationError:
        return None
    if account.connection_type != WhatsAppAccount.ConnectionType.API:
        return None
    return reverse("whatsapp-chat-detail", kwargs={"lead_id": lead.pk}) + f"?account={account.pk}"


def _crm_lead_url(lead):
    if lead is None:
        return None
    return reverse("crm-dashboard") + f"?pipeline={lead.pipeline_id}&lead={lead.pk}"


def delivery_counts(rows):
    delivered = Q(delivered_at__isnull=False) | Q(read_at__isnull=False)
    sent = Q(sent_at__isnull=False) | delivered
    accepted = Q(accepted_at__isnull=False) | sent
    counts = rows.aggregate(
        total=Count("pk"), accepted=Count("pk", filter=accepted), sent=Count("pk", filter=sent),
        delivered=Count("pk", filter=delivered), read=Count("pk", filter=Q(read_at__isnull=False)),
        replied=Count("pk", filter=Q(replied_at__isnull=False)), failed=Count("pk", filter=Q(state="failed")),
        skipped=Count("pk", filter=Q(state="skipped")), review=Count("pk", filter=Q(state="review")),
        sending=Count("pk", filter=Q(state="sending")), queued=Count("pk", filter=Q(state="pending", attempt_count=0)),
        retry_pending=Count("pk", filter=Q(state="pending", attempt_count__gt=0)), last_change=Max("updated_at"),
    )
    counts["last_change"] = iso(counts["last_change"])
    counts["percentages"] = {key: percent(counts[key], counts["total"]) for key in ("accepted", "sent", "delivered", "read", "replied", "failed", "skipped")}
    counts["dispatch_percent"] = percent(counts["total"] - counts["queued"] - counts["retry_pending"] - counts["sending"], counts["total"])
    return counts


def legacy_counts(campaign):
    rows = campaign.recipients
    counts = rows.aggregate(
        total=Count("pk"), accepted=Count("pk", filter=Q(message__status__in=["sent", "delivered", "read"])),
        delivered=Count("pk", filter=Q(message__status__in=["delivered", "read"])),
        read=Count("pk", filter=Q(message__status="read")),
        failed=Count("pk", filter=Q(message__status="failed") | Q(status="failed")),
        skipped=Count("pk", filter=Q(status="skipped")),
        queued=Count("pk", filter=Q(status="pending") & ~Q(message__status__in=["sent", "delivered", "read", "failed"])),
        last_change=Max("updated_at"),
    )
    # There was no attempt/reply ledger in the legacy sender. Do not display
    # invented zeroes or infer successful delivery from its recipient.sent flag.
    counts.update(sent=None, replied=None, review=None, sending=None, retry_pending=None, dispatch_percent=None)
    counts["last_change"] = iso(counts["last_change"])
    counts["percentages"] = {key: percent(counts[key], counts["total"]) if counts.get(key) is not None else None for key in ("accepted", "sent", "delivered", "read", "replied", "failed", "skipped")}
    return counts


def campaign_report(campaign, user):
    plan = getattr(campaign, "campaign_plan", None)
    counts = delivery_counts(campaign.campaign_delivery_rows) if plan else legacy_counts(campaign)
    state = campaign.status
    if plan:
        if plan.cancelled_at:
            state = "cancelled"
        elif plan.prepare_error:
            state = "failed"
        elif not plan.prepared_at:
            state = "preparing"
        elif counts["sending"]:
            state = "sending"
        elif counts["queued"] or counts["retry_pending"]:
            state = "scheduled" if plan.scheduled_for > timezone.now() and not campaign.started_at else "retrying" if not counts["queued"] else "queued"
        elif counts["review"]:
            state = "review"
        elif counts["total"] and counts["failed"] == counts["total"]:
            state = "failed"
        else:
            state = "completed"
    can_manage = False
    try:
        require_manage(user, campaign)
        can_manage = True
    except Exception as exc:
        from django.core.exceptions import PermissionDenied

        if not isinstance(exc, PermissionDenied):
            raise
    return {
        "id": str(campaign.pk), "name": campaign.name, "template_name": campaign.template_name or "Legacy text campaign",
        "account_name": campaign.account.business_name, "account_phone": campaign.account.display_phone_number,
        "account_id": str(campaign.account_id), "status": state, "counts": counts, "legacy": plan is None,
        "created_at": iso(campaign.created_at), "started_at": iso(campaign.started_at), "completed_at": iso(campaign.completed_at),
        "scheduled_for": iso(plan.scheduled_for) if plan else None, "timezone": plan.timezone if plan else None,
        "preview_body": campaign.body, "url": reverse("whatsapp-campaign-detail", kwargs={"campaign_id": campaign.pk}),
        "can_manage": can_manage and plan is not None,
        "preparation": {"processed": plan.prepare_cursor, "planned": plan.stats.get("eligible", 0),
                        "error": plan.prepare_error, "stats": {key: value for key, value in plan.stats.items() if key != "preparation_errors"},
                        "errors": plan.stats.get("preparation_errors", [])[:25]} if plan else None,
        "retry_policy": {"enabled": plan.auto_retry, "attempts": plan.retry_attempts, "delay_hours": plan.retry_delay_hours} if plan else None,
        "pricing": None,
    }


def filter_deliveries(*, campaign, user, filters):
    rows = campaign.campaign_delivery_rows.all()
    status = str(filters.get("status") or "")
    conditions = {
        "pending": Q(state="pending", attempt_count=0), "retry_pending": Q(state="pending", attempt_count__gt=0),
        "sending": Q(state="sending"), "accepted": Q(state="accepted", delivered_at__isnull=True, read_at__isnull=True),
        "delivered": Q(delivered_at__isnull=False) | Q(read_at__isnull=False), "read": Q(read_at__isnull=False),
        "replied": Q(replied_at__isnull=False), "failed": Q(state="failed"), "skipped": Q(state="skipped"), "review": Q(state="review"),
    }
    if status:
        if status not in conditions:
            raise CampaignInputError("Choose a valid recipient status.")
        rows = rows.filter(conditions[status])
    if filters.get("pipeline"):
        key = uuid_value(filters["pipeline"], "Pipeline filter")
        if not user_pipelines(user).filter(pk=key).exists():
            raise CampaignInputError("Pipeline filter is not available.")
        rows = rows.filter(Q(lead__pipeline_id=key) | Q(lead__isnull=True, pipeline_key=key))
    if filters.get("stage"):
        key = uuid_value(filters["stage"], "Stage filter")
        rows = rows.filter(Q(lead__stage_id=key) | Q(lead__isnull=True, stage_key=key))
    query = str(filters.get("q") or "").strip()[:150]
    if query:
        rows = rows.filter(Q(name__icontains=query) | Q(phone__icontains=query))
    return rows


def filter_legacy(*, campaign, filters):
    rows = campaign.recipients.select_related("lead__pipeline", "lead__stage", "message")
    state = str(filters.get("status") or "")
    if state:
        if state == "delivered":
            rows = rows.filter(message__status__in=["delivered", "read"])
        elif state in {"read", "failed"}:
            rows = rows.filter(message__status=state)
        elif state in {"skipped", "pending"}:
            rows = rows.filter(status=state)
        elif state == "accepted":
            rows = rows.filter(message__status="sent")
        else:
            rows = rows.none()
    if filters.get("pipeline"):
        rows = rows.filter(lead__pipeline_id=uuid_value(filters["pipeline"]))
    if filters.get("stage"):
        rows = rows.filter(lead__stage_id=uuid_value(filters["stage"]))
    if filters.get("q"):
        query = str(filters["q"])[:150]
        rows = rows.filter(Q(lead__name__icontains=query) | Q(lead__phone__icontains=query))
    return rows


def recipient_report(delivery, *, campaign, plan):
    lead = delivery.lead
    state = "read" if delivery.read_at else "delivered" if delivery.delivered_at else "retry_pending" if delivery.state == "pending" and delivery.attempt_count else delivery.state
    allowed, due, reason = retry_eligibility(delivery, plan, now=timezone.now())
    return {
        "id": str(delivery.pk), "lead_id": str(lead.pk) if lead else None, "name": delivery.name, "phone": delivery.phone,
        "pipeline": lead.pipeline.name if lead else delivery.pipeline_label, "stage": lead.stage.name if lead else delivery.stage_label,
        "status": state, "replied": delivery.replied_at is not None, "recorded_at": iso(delivery.updated_at),
        "error_code": delivery.error_code, "error_message": delivery.error_message, "attempt_count": delivery.attempt_count,
        "can_retry": allowed, "retry_at": iso(due), "retry_reason": reason,
        "chat_url": _campaign_chat_url(lead),
        "crm_url": _crm_lead_url(lead),
        "whatsapp_url": f"https://wa.me/{delivery.phone.lstrip('+')}",
        "times": {key: iso(getattr(delivery, f"{key}_at")) for key in ("accepted", "sent", "delivered", "read", "replied", "failed")},
        "attempts": [{"number": attempt.number, "provider_id": attempt.provider_id, "started_at": iso(attempt.started_at),
                      "error_code": attempt.error_code, "error_message": attempt.error_message, "uncertain": attempt.uncertain,
                      "accepted_at": iso(attempt.accepted_at), "delivered_at": iso(attempt.delivered_at), "read_at": iso(attempt.read_at)}
                     for attempt in delivery.attempts.all()],
    }


def legacy_recipient_report(recipient, *, campaign):
    message, lead = recipient.message, recipient.lead
    state = message.status if message else recipient.status
    if state == "sent":
        state = "accepted"
    return {"id": str(recipient.pk), "lead_id": str(lead.pk), "name": lead.name, "phone": lead.phone,
            "pipeline": lead.pipeline.name, "stage": lead.stage.name, "status": state, "replied": None,
            "recorded_at": iso(message.updated_at if message else recipient.updated_at), "error_code": "",
            "error_message": message.error if message else recipient.skip_reason, "attempt_count": None,
            "can_retry": False, "retry_at": None, "retry_reason": "Legacy sends have no safe attempt ledger. Create a new reviewed campaign to resend.",
            "chat_url": _campaign_chat_url(lead), "crm_url": _crm_lead_url(lead),
            "whatsapp_url": f"https://wa.me/{lead.phone.lstrip('+')}", "times": {}, "attempts": []}
