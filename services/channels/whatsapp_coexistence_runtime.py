"""Runtime bridge for Meta WhatsApp Business App Coexistence webhooks."""

import json
import logging
from functools import wraps

from apps.channels import views_flat
from services.channels.whatsapp_coexistence_service import (
    process_coexistence_webhook_payload,
)

logger = logging.getLogger(__name__)
_INSTALLED = False


def install_whatsapp_coexistence_runtime():
    """Extend the verified Meta webhook with Coexistence-only event handling.

    The legacy handler is called first. That preserves its HMAC verification and
    standard `messages`/`statuses` processing. We only inspect the payload after
    it has returned HTTP 200, so unsigned/invalid requests can never reach the
    Coexistence import path.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    original_delivery = views_flat._handle_webhook_delivery

    @wraps(original_delivery)
    def handle_webhook_delivery(request):
        response = original_delivery(request)
        if getattr(response, "status_code", 500) != 200:
            return response
        try:
            payload = json.loads(request.body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return response
        try:
            process_coexistence_webhook_payload(payload)
        except Exception:
            # Meta retries the entire webhook on non-2xx. Standard inbound/status
            # processing has already succeeded, so do not cause duplicate retry
            # storms because a history/echo importer hit an unexpected shape.
            logger.exception("WhatsApp Coexistence webhook processing failed.")
        return response

    views_flat._handle_webhook_delivery = handle_webhook_delivery
    _INSTALLED = True
