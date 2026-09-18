"""Persist verified webhook evidence before reconciliation.

Status callbacks are recorded by the existing verified webhook/failure wrapper,
including callbacks arriving before the send response stores its wamid. Incoming
message events are recorded transactionally when the canonical inbox saves them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as utc_timezone

from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.channels.campaign_models import CampaignAttempt, CampaignDelivery, CampaignEvent, CampaignPlan, CampaignSuppression
from apps.channels.models import WhatsAppMessage
from services.crm.lead_import_service import normalize_import_phone
from services.channels.whatsapp_error_service import describe_whatsapp_failure, failure_summary

from .campaign_delivery import maybe_schedule_retry, sync_legacy_recipient
from .campaign_policy import fingerprint


def _provider_time(value):
    try:
        parsed = datetime.fromtimestamp(int(value), tz=utc_timezone.utc)
        if parsed.year >= 2020 and parsed <= timezone.now() + timedelta(minutes=5):
            return parsed
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _phone(value):
    try:
        return normalize_import_phone(value)
    except Exception:
        return ""


def record_status_event(*, account, external_id, status, raw_payload):
    if status not in {"sent", "delivered", "read", "failed"} or not external_id or len(str(external_id)) > 128:
        return
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    message = WhatsAppMessage.objects.filter(external_id=external_id).only("pk", "account_id").first()
    if message is not None and message.account_id != account.pk:
        return
    if message is not None and not CampaignAttempt.objects.filter(message=message).exists():
        return
    # Store only status evidence, never the app token, request headers, or full
    # account payload. The failure service already sanitizes customer diagnostics.
    details = describe_whatsapp_failure(raw_payload=payload, error_text="") if status == "failed" else {}
    data = {"code": str(details.get("code") or "")[:64], "summary": failure_summary(details)[:2000] if details else ""}
    safe = {"account": str(account.pk), "id": str(external_id), "status": status, "timestamp": str(payload.get("timestamp") or ""),
            "recipient": _phone(payload.get("recipient_id")), "code": data["code"]}
    CampaignEvent.objects.get_or_create(digest=fingerprint(safe), defaults={
        "kind": "status", "account": account, "external_id": str(external_id), "message": message, "status": status,
        "recipient_phone": safe["recipient"], "occurred_at": _provider_time(payload.get("timestamp")), "data": data,
    })


@receiver(post_save, sender=WhatsAppMessage, dispatch_uid="campaign-inbound-evidence")
def record_inbound_event(sender, instance, created, raw=False, **kwargs):
    if raw or not created or instance.direction != "inbound" or not instance.external_id:
        return
    if instance.account.connection_type != "api" or instance.account.organization_id != instance.organization_id:
        return
    # Opt-out suppression is synchronous with the canonical inbound insert.
    # Campaign dispatch cannot wait for an AI response or a reporting poll.
    from apps.ai_engagement.services.runtime_state import is_explicit_opt_out

    phone = _phone(instance.from_number)
    if phone and is_explicit_opt_out(instance.body or ""):
        CampaignSuppression.objects.get_or_create(
            organization_id=instance.organization_id, phone=phone,
            defaults={"reason": "Recipient requested no further contact.", "source_message": instance},
        )
    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    CampaignEvent.objects.get_or_create(digest=fingerprint({"inbound": str(instance.pk)}), defaults={
        "kind": "inbound", "account_id": instance.account_id, "message": instance, "external_id": instance.external_id,
        "recipient_phone": _phone(instance.from_number), "occurred_at": _provider_time(payload.get("timestamp")),
    })


def _first(current, incoming):
    return min(current, incoming) if current else incoming


def _apply_status(event):
    attempt_ref = CampaignAttempt.objects.filter(
        Q(provider_id=event.external_id) | Q(message__external_id=event.external_id),
        delivery__campaign__account_id=event.account_id,
    ).values("pk", "delivery_id", "delivery__campaign_id").first()
    if not attempt_ref:
        return False, None
    plan = CampaignPlan.objects.select_for_update(of=("self",)).select_related("campaign").get(pk=attempt_ref["delivery__campaign_id"])
    delivery = CampaignDelivery.objects.select_for_update(of=("self",)).select_related("lead").get(pk=attempt_ref["delivery_id"])
    attempt = CampaignAttempt.objects.select_for_update().get(pk=attempt_ref["pk"])
    if event.recipient_phone and event.recipient_phone != delivery.phone:
        event.data = {**event.data, "ignored": "recipient_mismatch"}
        return True, plan.pk
    moment = event.occurred_at or event.received_at
    field = f"{event.status}_at"
    setattr(attempt, field, _first(getattr(attempt, field), moment))
    if event.status in {"sent", "delivered", "read"}:
        setattr(delivery, field, _first(getattr(delivery, field), moment))
        if not attempt.provider_id:
            attempt.provider_id = event.external_id
        attempt.uncertain = False
        if event.status in {"delivered", "read"} or (attempt.number == delivery.attempt_count and not attempt.failed_at):
            delivery.state, delivery.due_at, delivery.uncertain = "accepted", None, False
            delivery.error_code, delivery.error_message = "", ""
    else:
        attempt.error_code = event.data.get("code", "")
        attempt.error_message = event.data.get("summary", "")
        attempt.uncertain = False
        if attempt.number == delivery.attempt_count and not (delivery.delivered_at or delivery.read_at or delivery.replied_at):
            delivery.state, delivery.failed_at, delivery.uncertain = "failed", moment, False
            delivery.error_code, delivery.error_message = attempt.error_code, attempt.error_message
            delivery.http_status = attempt.http_status
            if attempt.error_code == "131050":
                CampaignSuppression.objects.get_or_create(organization_id=plan.campaign.organization_id, phone=delivery.phone,
                                                          defaults={"reason": "Provider reported a recipient marketing opt-out."})
            maybe_schedule_retry(delivery, plan)
    attempt.save()
    delivery.save()
    sync_legacy_recipient(delivery)
    # The legacy inbox handler accepts callbacks in arrival order. Keep managed
    # campaign messages monotonic without changing unrelated WhatsApp traffic.
    if attempt.message_id:
        message_status = "read" if attempt.read_at else "delivered" if attempt.delivered_at else "failed" if attempt.failed_at else "sent"
        WhatsAppMessage.objects.filter(pk=attempt.message_id).update(status=message_status)
    return True, plan.pk


def _apply_inbound(event):
    message = event.message
    if message is None or not event.recipient_phone:
        return True, None
    if message.account.connection_type != "api" or message.account.organization_id != message.organization_id:
        return True, None
    from apps.ai_engagement.services.runtime_state import is_explicit_opt_out

    if is_explicit_opt_out(message.body or ""):
        CampaignSuppression.objects.get_or_create(organization_id=message.organization_id, phone=event.recipient_phone,
                                                  defaults={"reason": "Recipient requested no further contact.", "source_message": message})
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    parent = str(context.get("id") or "")
    moment = event.occurred_at or message.created_at
    query = CampaignAttempt.objects.filter(
        delivery__campaign__organization_id=message.organization_id,
        delivery__campaign__account_id=message.account_id, delivery__phone=event.recipient_phone,
        started_at__lte=moment + timedelta(seconds=2),
    )
    method = "direct_reply"
    if parent:
        attempt = query.filter(Q(provider_id=parent) | Q(message__external_id=parent)).select_related("delivery").first()
        if attempt is None:
            # Keep an early quoted reply until the HTTP response can bind its ID.
            known = WhatsAppMessage.objects.filter(external_id=parent).exists()
            return known or event.received_at < timezone.now() - timedelta(minutes=5), None
    else:
        method = "last_outbound_within_7_days"
        last = WhatsAppMessage.objects.filter(
            organization_id=message.organization_id, account_id=message.account_id, direction="outbound",
            to_number__in=[event.recipient_phone, event.recipient_phone.lstrip("+")],
            created_at__lte=moment + timedelta(seconds=2), created_at__gte=moment - timedelta(days=7),
            external_id__isnull=False,
        ).exclude(external_id="").order_by("-created_at", "-pk").first()
        if last is None:
            return True, None
        attempt = query.filter(message=last).select_related("delivery").first()
        if attempt is None:
            return True, None
    plan = CampaignPlan.objects.select_for_update().get(pk=attempt.delivery.campaign_id)
    delivery = CampaignDelivery.objects.select_for_update().get(pk=attempt.delivery_id)
    delivery.replied_at = _first(delivery.replied_at, moment)
    if delivery.state == "pending" and delivery.attempt_count:
        delivery.state, delivery.due_at = "accepted", None
    delivery.save(update_fields=["replied_at", "state", "due_at", "updated_at"])
    event.data = {"attribution": method, "delivery_id": str(delivery.pk)}
    sync_legacy_recipient(delivery)
    return True, plan.pk


def reconcile_events(limit=200):
    campaigns, applied = set(), 0
    # Unknown, unrelated status IDs must not starve known campaign callbacks.
    # An early callback becomes selectable once its HTTP response binds the ID.
    ids = list(CampaignEvent.objects.filter(
        Q(kind="inbound")
        | Q(external_id__in=CampaignAttempt.objects.exclude(provider_id=None).values("provider_id"))
        | Q(external_id__in=WhatsAppMessage.objects.filter(campaign_attempt__isnull=False).exclude(external_id=None).values("external_id")),
        applied_at__isnull=True,
    ).order_by("received_at").values_list("pk", flat=True)[:limit])
    for event_id in ids:
        with transaction.atomic():
            event = CampaignEvent.objects.select_for_update(skip_locked=True, of=("self",)).select_related("message__account").filter(pk=event_id, applied_at__isnull=True).first()
            if event is None:
                continue
            complete, campaign_id = _apply_status(event) if event.kind == "status" else _apply_inbound(event)
            if complete:
                event.applied_at = timezone.now()
                event.save(update_fields=["applied_at", "data"])
                applied += 1
            if campaign_id:
                campaigns.add(campaign_id)
    return applied, campaigns


def install_campaign_webhook():
    """Extend only the existing, signature-verified Meta webhook.

    Account+WABA+sender identity is bound before storing evidence. Returning 503
    on a persistence error asks Meta to redeliver, rather than losing metrics.
    Existing inbound and campaign event unique keys make replay harmless.
    """
    import json
    from functools import wraps

    from django.http import HttpResponse
    from apps.channels import views_flat
    from apps.channels.models import WhatsAppAccount

    original = views_flat._handle_webhook_delivery
    if getattr(original, "_campaign_evidence_installed", False):
        return

    @wraps(original)
    def delivery(request):
        response = original(request)
        if response.status_code != 200:
            return response
        try:
            payload = json.loads(request.body)
        except (ValueError, TypeError):
            return response
        if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
            return response
        try:
            for entry in payload.get("entry") or []:
                if not isinstance(entry, dict):
                    continue
                for change in entry.get("changes") or []:
                    value = change.get("value") if isinstance(change, dict) else None
                    if not isinstance(value, dict):
                        continue
                    metadata = value.get("metadata") or {}
                    if not isinstance(metadata, dict):
                        continue
                    phone_id = str(metadata.get("phone_number_id") or "")
                    waba_id = str(entry.get("id") or "")
                    if not phone_id or not waba_id:
                        continue
                    # Multiple organizations cannot claim one provider identity.
                    accounts = list(WhatsAppAccount.objects.filter(
                        connection_type="api", phone_number_id=phone_id, waba_id=waba_id,
                    )[:2])
                    if len(accounts) != 1:
                        continue
                    for event in value.get("statuses") or []:
                        if isinstance(event, dict):
                            record_status_event(account=accounts[0], external_id=event.get("id"),
                                                status=event.get("status"), raw_payload=event)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Campaign webhook evidence could not be persisted")
            return HttpResponse("Please retry delivery", status=503)
        return response

    delivery._campaign_evidence_installed = True
    views_flat._handle_webhook_delivery = delivery
