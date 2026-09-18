"""Signed public Meta webhook boundary with durable broker-failure recovery."""
import hashlib
import hmac
import json
import logging
import re

from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from services.channels.instagram_service import instagram_app_secret, instagram_verify_token
from .instagram_models import InstagramWebhookDelivery
from .instagram_tasks import process_instagram_webhook_delivery_task

logger = logging.getLogger(__name__)


def _constant_time_equal(left, right):
    return hmac.compare_digest(str(left or "").encode("utf-8"), str(right or "").encode("utf-8"))


def _valid_signature(request):
    secret = instagram_app_secret()
    header = request.headers.get("X-Hub-Signature-256", "")
    if not secret or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", header):
        return False
    expected = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).hexdigest()
    return _constant_time_equal(expected, header[7:].lower())


@csrf_exempt
@require_http_methods(["GET", "POST"])
def instagram_webhook_view(request):
    if request.method == "GET":
        expected = instagram_verify_token()
        if (request.GET.get("hub.mode") == "subscribe" and expected and
                _constant_time_equal(request.GET.get("hub.verify_token"), expected)):
            return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
        return HttpResponseForbidden()
    if not _valid_signature(request):
        return HttpResponseForbidden()
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("Invalid JSON")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Invalid payload")
    digest = hashlib.sha256(request.body).hexdigest()

    # Commit the envelope before publishing. A worker on another connection can
    # always find it, and a broker failure must not roll back the incoming event.
    with transaction.atomic():
        delivery, _ = InstagramWebhookDelivery.objects.get_or_create(
            payload_sha256=digest, defaults={"raw_payload": payload},
        )

    try:
        # Serialize dispatches using the same row lock as the processor. Keep
        # the PROCESSING claim and broker submission in this short transaction:
        # a successful submission commits the claim; a failure rolls it back to
        # PENDING/FAILED so Meta's retry can recover it. The worker's row lock
        # waits for this commit before processing the already-durable envelope.
        with transaction.atomic():
            delivery = InstagramWebhookDelivery.objects.select_for_update().get(pk=delivery.pk)
            if delivery.status in {
                InstagramWebhookDelivery.Status.PROCESSING,
                InstagramWebhookDelivery.Status.PROCESSED,
                InstagramWebhookDelivery.Status.IGNORED,
            }:
                return HttpResponse("EVENT_RECEIVED")
            delivery.status = InstagramWebhookDelivery.Status.PROCESSING
            delivery.error_message = ""
            delivery.processed_at = None
            delivery.save(update_fields=["status", "error_message", "processed_at"])
            process_instagram_webhook_delivery_task.delay(str(delivery.pk))
    except Exception:
        # If the broker accepted a publish but its acknowledgement was lost,
        # another task may be submitted on retry. The existing worker locks and
        # idempotent message writes remain the final duplicate-processing guard.
        logger.warning("Instagram webhook enqueue unavailable for delivery %s", delivery.pk)
        response = HttpResponse("RETRY_LATER", status=503)
        response["Retry-After"] = "5"
        return response
    return HttpResponse("EVENT_RECEIVED")


# Ensure the first transaction commits even when ATOMIC_REQUESTS is enabled.
instagram_webhook_view = transaction.non_atomic_requests(instagram_webhook_view)
