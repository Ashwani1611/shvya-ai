"""Security boundary for the public Meta WhatsApp webhook endpoint."""

import hashlib
import hmac
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import views_flat

logger = logging.getLogger(__name__)


def _constant_time_equal(left, right):
    left = str(left or "")
    right = str(right or "")
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _valid_delivery_signature(request):
    """Fail closed unless a configured Meta app secret validates the body."""
    app_secret = str(getattr(settings, "META_APP_SECRET", "") or "").strip()
    if not app_secret:
        logger.error(
            "WhatsApp webhook rejected: META_APP_SECRET is not configured."
        )
        return False

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"),
        request.body,
        hashlib.sha256,
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook_secure_view(request):
    """
    Authenticate Meta before handing delivery to the existing webhook handler.

    GET verification uses constant-time token comparison. POST delivery fails
    closed when META_APP_SECRET is absent and verifies X-Hub-Signature-256
    before any webhook processing occurs.
    """
    if request.method == "GET":
        mode = request.GET.get("hub.mode", "")
        token = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")
        expected_token = str(
            getattr(settings, "META_VERIFY_TOKEN", "") or ""
        ).strip()

        if (
            mode == "subscribe"
            and expected_token
            and _constant_time_equal(token, expected_token)
        ):
            return HttpResponse(challenge)

        logger.warning("WhatsApp webhook verification failed.")
        return HttpResponseForbidden()

    if not _valid_delivery_signature(request):
        logger.warning("WhatsApp webhook: signature verification failed.")
        return HttpResponseForbidden()

    # The legacy handler still performs its own verification when a secret is
    # configured. Keeping that defense in depth avoids changing processing,
    # idempotency, or delivery behavior in this hardening patch.
    return views_flat.whatsapp_webhook_view(request)
