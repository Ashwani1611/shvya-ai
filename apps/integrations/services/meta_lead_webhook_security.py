"""Fail-closed signature verification for the Meta Lead Ads webhook.

The legacy Meta lead view historically accepted webhook POSTs even when the
application secret, signature header, or HMAC did not validate.  This module is
installed from IntegrationsConfig.ready() and wraps that existing view so POST
requests are authenticated before any lead lookup/import can run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from functools import wraps

from django.conf import settings
from django.http import HttpResponse

from apps.integrations.models import MetaLeadPage

logger = logging.getLogger(__name__)

_INSTALLED = False
_VERIFIED_REQUEST_ATTR = "_shvya_meta_lead_signature_verified"


def _configured_secrets(payload: dict) -> tuple[str, ...]:
    """Return non-empty app secrets that may legitimately sign this delivery."""

    secrets: list[str] = []

    global_secret = str(getattr(settings, "META_APP_SECRET", "") or "").strip()
    if global_secret:
        secrets.append(global_secret)

    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        entries = []

    page_ids = {
        str(entry.get("id") or "").strip()
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    }
    if page_ids:
        pages = MetaLeadPage.objects.filter(page_id__in=page_ids, is_active=True)
        for page in pages:
            secret = str(page.get_app_secret() or "").strip()
            if secret:
                secrets.append(secret)

    # Preserve order but do not perform duplicate HMAC work.
    return tuple(dict.fromkeys(secrets))


def _signature_matches_secret(request, secret: str) -> bool:
    """Validate Meta's SHA-256 signature, with legacy SHA-1 compatibility."""

    secret = str(secret or "").strip()
    if not secret:
        return False

    signature_256 = str(request.headers.get("X-Hub-Signature-256", "") or "").strip()
    if signature_256:
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), request.body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature_256, expected)

    signature_sha1 = str(request.headers.get("X-Hub-Signature", "") or "").strip()
    if signature_sha1:
        expected = "sha1=" + hmac.new(
            secret.encode("utf-8"), request.body, hashlib.sha1
        ).hexdigest()
        return hmac.compare_digest(signature_sha1, expected)

    return False


def _strict_signature_is_valid(request, secret) -> bool:
    """Replacement for the legacy helper: missing inputs always fail closed."""

    if getattr(request, _VERIFIED_REQUEST_ATTR, False):
        return True
    return _signature_matches_secret(request, str(secret or ""))


def _secure_meta_lead_webhook(original_view):
    @wraps(original_view)
    def secured(request, *args, **kwargs):
        # Meta's GET verification handshake is token-based and must remain
        # unchanged.  Other unsupported methods are also left to the view.
        if request.method != "POST":
            return original_view(request, *args, **kwargs)

        try:
            payload = json.loads(request.body or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            # Keep the existing 400 contract for malformed JSON rather than
            # converting malformed requests into an authentication response.
            return original_view(request, *args, **kwargs)

        if not isinstance(payload, dict):
            return original_view(request, *args, **kwargs)

        secrets = _configured_secrets(payload)
        if not secrets:
            logger.error(
                "Rejected Meta Lead Ads webhook because no app secret is configured"
            )
            return HttpResponse("Webhook signature verification unavailable", status=403)

        if not any(_signature_matches_secret(request, secret) for secret in secrets):
            logger.warning("Rejected Meta Lead Ads webhook with invalid or missing signature")
            return HttpResponse("Invalid webhook signature", status=403)

        # The legacy view performs its own signature check. Mark this request as
        # already authenticated so it cannot emit the old fail-open warning or
        # disagree when several configured Pages share one callback endpoint.
        setattr(request, _VERIFIED_REQUEST_ATTR, True)
        return original_view(request, *args, **kwargs)

    secured._shvya_meta_lead_signature_guard = True
    return secured


def install_meta_lead_webhook_security() -> None:
    """Install the fail-closed guard exactly once per Django process."""

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.integrations.views import meta_leads

    if not getattr(
        meta_leads.meta_lead_webhook,
        "_shvya_meta_lead_signature_guard",
        False,
    ):
        meta_leads.meta_lead_webhook = _secure_meta_lead_webhook(
            meta_leads.meta_lead_webhook
        )

    meta_leads._signature_is_valid = _strict_signature_is_valid
    _INSTALLED = True
