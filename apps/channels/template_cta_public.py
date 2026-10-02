"""Public, scanner-resistant action page for tracked WhatsApp template CTAs."""

from __future__ import annotations

import hashlib
import re
import secrets
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.core import signing
from django.db import models, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils.crypto import constant_time_compare, salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from apps.channels.models import WhatsAppTemplate
from apps.channels.template_cta_models import WhatsAppTemplateCTAEvent
from services.channels.template_cta_tracking import (
    TRACKING_PATH,
    decode_cta_token,
)


VISITOR_COOKIE = "shvya_wa_cta"
FORM_SALT = "shvya.whatsapp.template.cta.form.v1"
FORM_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_PHONE = re.compile(r"^\+?[0-9() .-]{5,32}$")


def _no_store(response):
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    response["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    )
    return response


def _visitor_hash(value):
    return salted_hmac(
        "shvya.whatsapp.template.cta.visitor.v1",
        str(value or ""),
        secret=settings.SECRET_KEY,
        algorithm="sha256",
    ).hexdigest()


def _visitor(request):
    value = str(request.COOKIES.get(VISITOR_COOKIE) or "").strip()
    created = False
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,96}", value):
        value = secrets.token_urlsafe(32)
        created = True
    return value, created


def _replace_placeholder(destination, placeholder, value):
    if not placeholder:
        return destination
    if value is None:
        raise Http404("This tracked button is missing its destination value.")
    pattern = re.compile(r"{{\s*" + re.escape(str(placeholder)) + r"\s*}}")
    if not pattern.search(destination):
        raise Http404("This tracked button is invalid.")
    # Treat the message-time value as one URL component. Encoding reserved
    # characters prevents a CRM value from injecting a second query parameter,
    # changing the host, or altering the signed destination's structure.
    encoded = quote(str(value), safe="-._~")
    return pattern.sub(encoded, destination, count=1)


def _resolved_action(data, dynamic_value):
    action_type = str(data.get("a") or "")
    destination = _replace_placeholder(
        str(data.get("d") or ""),
        str(data.get("p") or ""),
        dynamic_value,
    )
    label = str(data.get("l") or "Action")[:80]

    if action_type == WhatsAppTemplateCTAEvent.ActionType.URL:
        try:
            parsed = urlsplit(destination)
        except ValueError as exc:
            raise Http404("This website action is invalid.") from exc
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise Http404("This website action is invalid.")
        return action_type, destination, label

    if action_type == WhatsAppTemplateCTAEvent.ActionType.CALL:
        if not _PHONE.fullmatch(destination):
            raise Http404("This call action is invalid.")
        digits = re.sub(r"\D", "", destination)
        dial = f"+{digits}" if destination.startswith("+") else digits
        return action_type, f"tel:{dial}", label

    if action_type == WhatsAppTemplateCTAEvent.ActionType.COPY_CODE:
        if not destination or len(destination) > 256:
            raise Http404("This copy-code action is invalid.")
        return action_type, destination, label

    raise Http404("This action is not supported.")


def _digest(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _form_token(*, cta_token, visitor_hash, destination):
    return signing.dumps(
        {
            "h": _digest(cta_token),
            "v": visitor_hash,
            "d": _digest(destination),
            "r": secrets.token_urlsafe(24),
        },
        salt=FORM_SALT,
        compress=True,
    )


def _verify_form_token(*, form_token, cta_token, visitor_hash, destination):
    try:
        data = signing.loads(
            str(form_token or ""),
            salt=FORM_SALT,
            max_age=FORM_MAX_AGE_SECONDS,
        )
    except signing.BadSignature as exc:
        raise Http404("This action confirmation has expired.") from exc
    if (
        not isinstance(data, dict)
        or not constant_time_compare(str(data.get("h") or ""), _digest(cta_token))
        or not constant_time_compare(str(data.get("v") or ""), visitor_hash)
        or not constant_time_compare(
            str(data.get("d") or ""),
            _digest(destination),
        )
        or not data.get("r")
    ):
        raise Http404("This action confirmation is invalid.")
    return data


def _render(
    request,
    *,
    template,
    action_type,
    destination,
    label,
    form_token,
    confirmed=False,
):
    response = render(
        request,
        "channels/template_cta_action.html",
        {
            "business_name": template.account.business_name
            or template.organization.name,
            "action_type": action_type,
            "action_label": label,
            "destination": destination,
            "form_token": form_token,
            "confirmed": confirmed,
        },
    )
    return _no_store(response)


@never_cache
@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def tracked_template_cta(request, token, suffix=None):
    try:
        data = decode_cta_token(token)
    except signing.BadSignature as exc:
        raise Http404("This tracked action is invalid.") from exc

    template = (
        WhatsAppTemplate.objects.select_related("organization", "account")
        .filter(
            pk=data.get("t"),
            account__organization_id=models.F("organization_id"),
        )
        .first()
    )
    if template is None:
        raise Http404("This tracked action is no longer available.")

    dynamic_value = suffix or request.GET.get("v") or request.POST.get("v")
    action_type, destination, label = _resolved_action(data, dynamic_value)
    visitor, new_visitor = _visitor(request)
    visitor_hash = _visitor_hash(visitor)

    if request.method == "GET":
        form_token = _form_token(
            cta_token=token,
            visitor_hash=visitor_hash,
            destination=destination,
        )
        response = _render(
            request,
            template=template,
            action_type=action_type,
            destination=destination,
            label=label,
            form_token=form_token,
        )
        if new_visitor:
            response.set_cookie(
                VISITOR_COOKIE,
                visitor,
                max_age=365 * 24 * 60 * 60,
                secure=request.is_secure(),
                httponly=True,
                samesite="Lax",
                path=TRACKING_PATH,
            )
        return response

    form_data = _verify_form_token(
        form_token=request.POST.get("event_token"),
        cta_token=token,
        visitor_hash=visitor_hash,
        destination=destination,
    )
    request_key = hashlib.sha256(
        f"{form_data['r']}:{visitor_hash}".encode("utf-8")
    ).hexdigest()
    stored_destination = (
        str(data.get("d") or "")
        if action_type != WhatsAppTemplateCTAEvent.ActionType.COPY_CODE
        else "[copy-code]"
    )
    with transaction.atomic():
        WhatsAppTemplateCTAEvent.objects.get_or_create(
            request_key=request_key,
            defaults={
                "organization": template.organization,
                "account": template.account,
                "template": template,
                "action_type": action_type,
                "button_index": int(data.get("b") or 0),
                "card_index": data.get("c"),
                "button_label": label,
                "destination": stored_destination,
                "visitor_hash": visitor_hash,
            },
        )

    payload = {
        "ok": True,
        "action_type": action_type,
        "action_label": label,
        "destination": destination,
    }
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return _no_store(JsonResponse(payload))

    return _render(
        request,
        template=template,
        action_type=action_type,
        destination=destination,
        label=label,
        form_token="",
        confirmed=True,
    )
