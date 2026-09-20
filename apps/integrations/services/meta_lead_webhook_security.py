"""Signature helpers for the canonical Meta Lead Ads webhook boundary."""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings

from apps.integrations.models import MetaLeadPage


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

    return tuple(dict.fromkeys(secrets))


def _signature_matches_secret(request, secret: str) -> bool:
    """Validate Meta's request HMAC and fail closed for missing inputs."""
    secret = str(secret or "").strip()
    if not secret:
        return False

    signature_256 = str(
        request.headers.get("X-Hub-Signature-256", "") or ""
    ).strip()
    if signature_256:
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            request.body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature_256, expected)

    # Retain legacy SHA-1 compatibility only for deployments still receiving
    # that header. Missing signatures still fail closed.
    signature_sha1 = str(
        request.headers.get("X-Hub-Signature", "") or ""
    ).strip()
    if signature_sha1:
        expected = "sha1=" + hmac.new(
            secret.encode("utf-8"),
            request.body,
            hashlib.sha1,
        ).hexdigest()
        return hmac.compare_digest(signature_sha1, expected)

    return False
