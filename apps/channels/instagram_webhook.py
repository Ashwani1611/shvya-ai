"""Signed public Meta Instagram webhook boundary."""

import hashlib
import hmac
import json
import logging

from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from services.channels.instagram_service import instagram_app_secret, instagram_verify_token

from .instagram_models import InstagramWebhookDelivery
from .instagram_tasks import process_instagram_webhook_delivery_task

logger = logging.getLogger(__name__)


def _constant_time_equal(left, right):
    return hmac.compare_digest(
        str(left or "").encode("utf-8"),
        str(right or "").encode("utf-8"),
    )


def _valid_signature(request) -> bool:
    secret = instagram_app_secret()
    if not secret:
        logger.error("Instagram webhook rejected: Meta app secret is not configured.")
        return False
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@csrf_exempt
@require_http_methods(["GET", "POST"])
def instagram_webhook_view(request):
    """Verify Meta synchronously, persist the delivery, process asynchronously."""
    if request.method == "GET":
        mode = request.GET.get("hub.mode", "")
        token = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")
        expected = instagram_verify_token()
        if mode == "subscribe" and expected and _constant_time_equal(token, expected):
            return HttpResponse(challenge)
        logger.warning("Instagram webhook verification failed.")
        return HttpResponseForbidden()

    if not _valid_signature(request):
        logger.warning("Instagram webhook signature verification failed.")
        return HttpResponseForbidden()

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("Invalid JSON")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Invalid payload")

    digest = hashlib.sha256(request.body).hexdigest()
    with transaction.atomic():
        delivery, created = InstagramWebhookDelivery.objects.get_or_create(
            payload_sha256=digest,
            defaults={"raw_payload": payload},
        )
        should_enqueue = created
        if not created and delivery.status == InstagramWebhookDelivery.Status.FAILED:
            delivery.status = InstagramWebhookDelivery.Status.PENDING
            delivery.error_message = ""
            delivery.raw_payload = payload
            delivery.processed_at = None
            delivery.save(
                update_fields=["status", "error_message", "raw_payload", "processed_at"]
            )
            should_enqueue = True
        if should_enqueue:
            transaction.on_commit(
                lambda delivery_id=str(delivery.id): process_instagram_webhook_delivery_task.delay(
                    delivery_id
                )
            )

    # Meta expects a fast 2xx acknowledgement; processing is durable in Celery.
    return HttpResponse("EVENT_RECEIVED", status=200)
