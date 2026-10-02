"""Public, token-only WhatsApp template CTA tracking endpoints."""

import hashlib
import hmac
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponse, HttpResponseGone, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_deny
from django.views.decorators.http import require_http_methods

from .tracking_models import (
    WhatsAppTemplateTrackedClick,
    WhatsAppTemplateTrackedLink,
)


_BOT_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "preview",
    "facebookexternalhit",
    "meta-externalagent",
    "headlesschrome",
    "curl/",
    "wget/",
    "python-requests",
)
_DEDUPE_SECONDS = 3


def _client_address(request):
    forwarded = str(request.META.get("HTTP_CF_CONNECTING_IP") or "").strip()
    if not forwarded:
        forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",", 1)[0].strip()
    return forwarded or str(request.META.get("REMOTE_ADDR") or "").strip()


def _fingerprint(request, link):
    value = "|".join(
        [
            str(link.token),
            _client_address(request),
            str(request.META.get("HTTP_USER_AGENT") or "")[:500],
        ]
    )
    key = str(settings.SECRET_KEY).encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _is_automated(request):
    agent = str(request.META.get("HTTP_USER_AGENT") or "").strip().casefold()
    if not agent:
        return True
    return any(marker in agent for marker in _BOT_MARKERS)


def _record_click(request, link):
    if request.method != "GET" or _is_automated(request):
        return False
    fingerprint = _fingerprint(request, link)
    cutoff = timezone.now() - timedelta(seconds=_DEDUPE_SECONDS)
    duplicate = WhatsAppTemplateTrackedClick.objects.filter(
        link=link,
        event_type=WhatsAppTemplateTrackedClick.EventType.CLICK,
        fingerprint=fingerprint,
        created_at__gte=cutoff,
    ).exists()
    if duplicate:
        return False
    WhatsAppTemplateTrackedClick.objects.create(
        link=link,
        event_type=WhatsAppTemplateTrackedClick.EventType.CLICK,
        fingerprint=fingerprint,
        user_agent=str(request.META.get("HTTP_USER_AGENT") or "")[:500],
    )
    return True


def _secured(response):
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@xframe_options_deny
@require_http_methods(["GET", "HEAD"])
def tracked_template_cta(request, token):
    """Count a real CTA request, then perform the server-held action."""

    link = get_object_or_404(
        WhatsAppTemplateTrackedLink.objects.select_related("organization"),
        token=token,
        is_active=True,
    )
    _record_click(request, link)

    if link.action_type == WhatsAppTemplateTrackedLink.ActionType.WEBSITE:
        try:
            parsed = urlsplit(link.destination_url)
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
        ):
            return _secured(HttpResponseGone("This tracked destination is no longer available."))
        return _secured(HttpResponseRedirect(link.destination_url))

    if link.action_type == WhatsAppTemplateTrackedLink.ActionType.CALL:
        if not link.phone_number:
            return _secured(HttpResponseGone("This call action is no longer available."))
        response = render(
            request,
            "channels/whatsapp_cta_action.html",
            {
                "action_type": "call",
                "button_text": link.button_text or "Call now",
                "phone_number": link.phone_number,
                "organization_name": link.organization.name,
            },
        )
        return _secured(response)

    if link.action_type == WhatsAppTemplateTrackedLink.ActionType.COPY_CODE:
        if not link.coupon_code:
            return _secured(HttpResponseGone("This code is no longer available."))
        response = render(
            request,
            "channels/whatsapp_cta_action.html",
            {
                "action_type": "copy_code",
                "button_text": link.button_text or "Copy code",
                "coupon_code": link.coupon_code,
                "organization_name": link.organization.name,
            },
        )
        return _secured(response)

    return _secured(HttpResponse(status=204))
