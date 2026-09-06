import logging

import requests
from celery import shared_task
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.integrations.models import WebhookDelivery
from apps.integrations.services.webhook import (
    WEBHOOK_DELIVERY_HEADER,
    WEBHOOK_SECRET_HEADER,
    WEBHOOK_USER_AGENT,
    assert_public_webhook_target,
)

logger = logging.getLogger(__name__)

RETRY_DELAYS = (30, 60)
WEBHOOK_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BODY_LENGTH = 2000


def _save_attempt(delivery, *, status, response_status=None, response_body="", error=""):
    delivery.status = status
    delivery.response_status = response_status
    delivery.response_body = (response_body or "")[:MAX_RESPONSE_BODY_LENGTH]
    delivery.error_message = (error or "")[:MAX_RESPONSE_BODY_LENGTH]
    delivery.save(
        update_fields=[
            "status",
            "attempt_count",
            "response_status",
            "response_body",
            "error_message",
            "delivered_at",
            "updated_at",
        ]
    )


def _retry_or_fail(task, delivery, reason):
    retry_index = task.request.retries

    if retry_index < len(RETRY_DELAYS):
        _save_attempt(
            delivery,
            status=WebhookDelivery.Status.RETRYING,
            response_status=delivery.response_status,
            response_body=delivery.response_body,
            error=reason,
        )
        raise task.retry(
            exc=RuntimeError(reason),
            countdown=RETRY_DELAYS[retry_index],
        )

    _save_attempt(
        delivery,
        status=WebhookDelivery.Status.FAILED,
        response_status=delivery.response_status,
        response_body=delivery.response_body,
        error=reason,
    )
    return {
        "status": "failed",
        "delivery_id": str(delivery.id),
        "reason": reason,
    }


@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    name="apps.integrations.tasks.deliver_webhook_task",
)
def deliver_webhook_task(self, delivery_id):
    delivery = (
        WebhookDelivery.objects.select_related("webhook")
        .filter(id=delivery_id)
        .first()
    )

    if delivery is None:
        return {"status": "missing", "delivery_id": str(delivery_id)}

    if delivery.status == WebhookDelivery.Status.SENT:
        return {"status": "already_sent", "delivery_id": str(delivery.id)}

    webhook = delivery.webhook

    if not webhook.is_enabled:
        delivery.attempt_count += 1
        _save_attempt(
            delivery,
            status=WebhookDelivery.Status.FAILED,
            error="Webhook was disabled before this delivery could be sent.",
        )
        return {"status": "disabled", "delivery_id": str(delivery.id)}

    secret = webhook.get_secret()
    if not secret:
        delivery.attempt_count += 1
        _save_attempt(
            delivery,
            status=WebhookDelivery.Status.FAILED,
            error="Webhook secret is unavailable or could not be decrypted.",
        )
        return {"status": "invalid_secret", "delivery_id": str(delivery.id)}

    delivery.attempt_count += 1
    delivery.response_status = None
    delivery.response_body = ""
    delivery.error_message = ""

    try:
        target_url = assert_public_webhook_target(webhook.endpoint_url)
    except ValidationError as exc:
        reason = "; ".join(exc.messages)
        return _retry_or_fail(self, delivery, reason)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": WEBHOOK_USER_AGENT,
        WEBHOOK_SECRET_HEADER: secret,
        WEBHOOK_DELIVERY_HEADER: str(delivery.id),
    }

    try:
        response = requests.post(
            target_url,
            json=delivery.payload,
            headers=headers,
            timeout=WEBHOOK_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        logger.warning(
            "Webhook delivery %s request failed: %s",
            delivery.id,
            exc,
        )
        return _retry_or_fail(self, delivery, str(exc))

    delivery.response_status = response.status_code
    delivery.response_body = response.text[:MAX_RESPONSE_BODY_LENGTH]

    if 200 <= response.status_code < 300:
        delivery.status = WebhookDelivery.Status.SENT
        delivery.error_message = ""
        delivery.delivered_at = timezone.now()
        delivery.save(
            update_fields=[
                "status",
                "attempt_count",
                "response_status",
                "response_body",
                "error_message",
                "delivered_at",
                "updated_at",
            ]
        )
        return {
            "status": "sent",
            "delivery_id": str(delivery.id),
            "response_status": response.status_code,
        }

    reason = f"Webhook endpoint returned HTTP {response.status_code}."
    return _retry_or_fail(self, delivery, reason)
