"""Leased, bounded delivery of immutable Meta events on the general worker."""

import logging
import re
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.integrations.models import MetaConversionDelivery as Delivery
from apps.integrations.models import MetaConversionsConfiguration as Configuration

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 6
RETRY_DELAYS = (30, 120, 600, 1800, 3600)
TERMINAL_STATUSES = {Delivery.Status.SENT, Delivery.Status.FAILED, Delivery.Status.SKIPPED}


def _finish(delivery, **values):
    values.update(lease_until=None, lease_token=None, updated_at=timezone.now())
    return Delivery.objects.filter(
        pk=delivery.pk, organization_id=delivery.organization_id,
        status=Delivery.Status.SENDING, lease_token=delivery.lease_token,
    ).update(**values)


def _failure(delivery, reason, *, transient=False, response_status=None, code=None, trace_id=""):
    retry = transient and delivery.attempt_count < MAX_ATTEMPTS
    _finish(
        delivery, status=Delivery.Status.RETRYING if retry else Delivery.Status.FAILED,
        error_message=reason, response_status=response_status, meta_error_code=code,
        trace_id=trace_id, last_enqueued_at=None,
        next_attempt_at=timezone.now() + timedelta(seconds=RETRY_DELAYS[min(delivery.attempt_count - 1, 4)]),
    )
    return {"status": "retrying" if retry else "failed", "delivery_id": str(delivery.pk)}


def deliver_event(delivery_id):
    now = timezone.now()
    with transaction.atomic():
        delivery = Delivery.objects.select_for_update(of=("self",)).select_related("configuration", "mapping").filter(pk=delivery_id).first()
        if delivery is None:
            return {"status": "missing"}
        if delivery.status in TERMINAL_STATUSES:
            return {"status": delivery.status}
        if delivery.status == Delivery.Status.SENDING and delivery.lease_until and delivery.lease_until > now:
            return {"status": "busy"}
        if delivery.next_attempt_at > now:
            return {"status": "not_due"}
        configuration = delivery.configuration
        if (delivery.organization_id != configuration.organization_id or
                delivery.dataset_id != configuration.dataset_id or
                delivery.destination_version != configuration.destination_version or
                delivery.mapping_id is None):
            delivery.status = Delivery.Status.SKIPPED
            delivery.error_message = ("The event mapping was removed before delivery." if delivery.mapping_id is None
                                      else "The dataset connection changed before delivery.")
            delivery.lease_until, delivery.lease_token = None, None
            delivery.save(update_fields=["status", "error_message", "lease_until", "lease_token", "updated_at"])
            return {"status": "skipped"}
        if not delivery.is_probe and (not configuration.is_enabled or
                                      (delivery.mapping and not delivery.mapping.is_enabled)):
            return {"status": "paused"}
        delivery.status = Delivery.Status.SENDING
        delivery.lease_until = now + timedelta(seconds=90)
        delivery.lease_token = uuid.uuid4()
        delivery.attempt_count += 1
        delivery.save(update_fields=["status", "lease_until", "lease_token", "attempt_count", "updated_at"])

    if delivery.attempt_count > MAX_ATTEMPTS:
        return _failure(delivery, "Automatic delivery attempts are exhausted. Retry after fixing the connection.")
    event_time = delivery.payload.get("event_time")
    if not isinstance(event_time, int) or event_time < int((now - timedelta(days=7)).timestamp()):
        return _failure(delivery, "This event is outside Meta's seven-day delivery window.")
    token = configuration.get_access_token()
    if not token:
        return _failure(delivery, "The access token is unavailable. Save a new Conversions API token.")
    version = str(getattr(settings, "META_CONVERSIONS_API_VERSION", "v26.0"))
    if not re.fullmatch(r"v[0-9]+\.0", version) or not re.fullmatch(r"[0-9]{5,30}", delivery.dataset_id):
        return _failure(delivery, "The API version or dataset ID is invalid.")
    body = {"data": [delivery.payload]}
    if delivery.is_test:
        if not delivery.test_event_code:
            return _failure(delivery, "A Test Events code is required for this test delivery.")
        body["test_event_code"] = delivery.test_event_code
    try:
        response = requests.post(
            f"https://graph.facebook.com/{version}/{delivery.dataset_id}/events",
            json=body, headers={"Authorization": f"Bearer {token}"},
            timeout=(5, 15), allow_redirects=False,
        )
    except requests.RequestException:
        # Exception strings, URLs, headers and raw responses must never enter logs.
        logger.warning("Meta event network failure delivery=%s org=%s", delivery.pk, delivery.organization_id)
        return _failure(delivery, "Meta could not be reached. Delivery will be retried automatically.", transient=True)
    try:
        result = response.json()
        if not isinstance(result, dict):
            result = {}
    except ValueError:
        result = {}
    trace = result.get("fbtrace_id", "")
    trace = trace if isinstance(trace, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", trace) else ""
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    if (200 <= response.status_code < 300 and not error and
            type(result.get("events_received")) is int and result["events_received"] == 1):
        committed = _finish(
            delivery, status=Delivery.Status.SENT, error_message="",
            response_status=response.status_code, meta_error_code=None,
            trace_id=trace, delivered_at=timezone.now(),
        )
        if committed:
            Configuration.objects.filter(
                pk=configuration.pk, destination_version=delivery.destination_version,
                encrypted_access_token=configuration.encrypted_access_token,
            ).update(verified_at=timezone.now())
        return {"status": "sent", "delivery_id": str(delivery.pk)}
    code = error.get("code")
    code = code if type(code) is int and 0 <= code <= 2147483647 else None
    if not trace:
        error_trace = error.get("fbtrace_id", "")
        trace = error_trace if isinstance(error_trace, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", error_trace) else ""
    transient = (response.status_code == 429 or response.status_code >= 500 or
                 error.get("is_transient") is True or code in {1, 2, 4, 17, 32, 613})
    if code == 190:
        reason, transient = "Meta rejected the access token. Generate and save a new token for this dataset.", False
    elif code in {10, 200, 294}:
        reason, transient = "The token cannot send events to this dataset. Check its asset access in Meta.", False
    elif code == 100:
        reason, transient = "Meta rejected the event parameters. Check the lead identifiers, mapping and event value.", False
    elif transient:
        reason = "Meta is temporarily unavailable or rate limiting events. Delivery will be retried."
    else:
        reason = "Meta did not accept this event. Check the dataset, token and event mapping."
    return _failure(delivery, reason, transient=transient, response_status=response.status_code, code=code, trace_id=trace)


def recover_due_events():
    from apps.integrations.services.meta_conversions import enqueue_delivery

    now = timezone.now()
    with transaction.atomic():
        rows = list(Delivery.objects.select_for_update(skip_locked=True, of=("self",)).filter(
            Q(configuration__is_enabled=True) | Q(is_probe=True),
            Q(mapping__is_enabled=True) | Q(mapping__isnull=True) | Q(is_probe=True),
            Q(last_enqueued_at__isnull=True) | Q(last_enqueued_at__lt=now - timedelta(seconds=90)),
            Q(status__in=[Delivery.Status.QUEUED, Delivery.Status.RETRYING]) |
            Q(status=Delivery.Status.SENDING, lease_until__lte=now),
            next_attempt_at__lte=now,
        ).order_by("next_attempt_at")[:100])
        Delivery.objects.filter(pk__in=[d.pk for d in rows]).update(last_enqueued_at=now)
        for delivery in rows:
            enqueue_delivery(delivery)
    return {"queued": len(rows)}
