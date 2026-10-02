"""Safety and Meta-compatibility guards for first-party CTA tracking."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from apps.channels.models import WhatsAppTemplate

from . import template_cta_tracking as tracking


_INSTALLED = False


def _dynamic_marker(destination, placeholder):
    if not placeholder:
        return None
    pattern = re.compile(
        r"{{\s*" + re.escape(str(placeholder)) + r"\s*}}"
    )
    matches = list(pattern.finditer(destination))
    if len(matches) != 1:
        return None
    return matches[0]


def _authority_end(destination):
    scheme_at = destination.find("://")
    if scheme_at < 0:
        return -1
    start = scheme_at + 3
    candidates = [
        index
        for index in (
            destination.find("/", start),
            destination.find("?", start),
            destination.find("#", start),
        )
        if index >= 0
    ]
    return min(candidates) if candidates else len(destination)


def install_template_cta_safety():
    """Patch tracking with fail-closed URL and category validation."""

    global _INSTALLED
    if _INSTALLED:
        return

    from .template_service import TemplateError

    original_definition = tracking._definition
    original_apply = tracking.apply_tracking_to_meta_payload
    original_has_trackable_cta = tracking.template_has_trackable_cta

    def definition(button):
        result = original_definition(button)
        if not result or result.get("action_type") != "url":
            return result

        destination = str(result.get("destination") or "").strip()
        try:
            parsed = urlsplit(
                tracking._VARIABLE.sub("example", destination, count=1)
            )
        except ValueError as exc:
            raise TemplateError("Visit Website requires a valid HTTPS URL.") from exc
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise TemplateError("Visit Website requires a valid HTTPS URL.")

        placeholder = str(result.get("placeholder") or "")
        if placeholder:
            marker = _dynamic_marker(destination, placeholder)
            if marker is None:
                raise TemplateError(
                    "A tracked website button needs one URL variable."
                )
            if destination[marker.end() :].strip():
                raise TemplateError(
                    "A dynamic website variable must be at the end of the URL."
                )
            if marker.start() < _authority_end(destination):
                raise TemplateError(
                    "A dynamic website variable cannot change the URL host."
                )
        return result

    def apply_tracking_to_meta_payload(*, template, payload, base):
        # Authentication templates have Meta-defined OTP/copy-code semantics.
        # Converting those buttons to URL actions would make the template
        # invalid and weaken the authentication user experience.
        if template.category == WhatsAppTemplate.Category.AUTHENTICATION:
            return payload
        return original_apply(template=template, payload=payload, base=base)

    def template_has_trackable_cta(template):
        if template.category == WhatsAppTemplate.Category.AUTHENTICATION:
            return False
        return original_has_trackable_cta(template)

    tracking._definition = definition
    tracking.apply_tracking_to_meta_payload = apply_tracking_to_meta_payload
    tracking.template_has_trackable_cta = template_has_trackable_cta
    WhatsAppTemplate.has_trackable_cta = property(template_has_trackable_cta)
    _INSTALLED = True
