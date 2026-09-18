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
    with transaction.atomic():
        delivery, _ = InstagramWebhookDelivery.objects.get_or_create(
            payload_sha256=digest, defaults={"raw_payload": payload},
        )
        delivery = InstagramWebhookDelivery.objects.select_for_update().get(pk=delivery.pk)
        if delivery.status in {InstagramWebhookDelivery.Status.PROCESSED, InstagramWebhookDelivery.Status.IGNORED}:
            return HttpResponse("EVENT_RECEIVED")
        if delivery.status == InstagramWebhookDelivery.Status.FAILED:
            delivery.status = InstagramWebhookDelivery.Status.PENDING
            delivery.error_message = ""
            delivery.processed_at = None
            delivery.save(update_fields=["status", "error_message", "processed_at"])
    # The transaction has committed before enqueue. A Meta retry can requeue an
    # existing PENDING row after broker failure. The worker locks/deduplicates it.
    # non_atomic_requests below keeps this boundary valid with ATOMIC_REQUESTS.
    try:
        process_instagram_webhook_delivery_task.delay(str(delivery.pk))
    except Exception:
        logger.warning("Instagram webhook enqueue unavailable for delivery %s", delivery.pk)
        return HttpResponse("RETRY_LATER", status=503)
    return HttpResponse("EVENT_RECEIVED")


instagram_webhook_view = transaction.non_atomic_requests(instagram_webhook_view)
