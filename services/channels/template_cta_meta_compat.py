"""Meta compatibility for SHVYA-tracked WhatsApp template buttons.

Meta's template-creation schema treats a dynamic URL button's ``example`` as
only the value that replaces ``{{1}}``. It is not the completed URL. The first
tracking implementation supplied the completed SHVYA URL, which Meta rejects
with the generic "Parameter value is not valid" response.

This module is installed after the core tracking and safety layers. It keeps
legacy tracking links readable while producing compact URL-safe tokens, emits
suffix-only examples for dynamic URL buttons, and exposes a HEAD-compatible
public action view for Meta's URL verifier.
"""

from __future__ import annotations

import base64
import binascii
import re

from django.http import HttpResponse

from . import template_cta_tracking as tracking


TOKEN_PREFIX = "t2_"
_META_EXAMPLE_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_INSTALLED = False


def _compact_token(value):
    encoded = base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii")
    return TOKEN_PREFIX + encoded.rstrip("=")


def _expand_token(value):
    token = str(value or "")
    if not token.startswith(TOKEN_PREFIX):
        return token
    encoded = token[len(TOKEN_PREFIX) :]
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        from django.core import signing

        raise signing.BadSignature("Invalid tracked CTA token.") from exc


def _first_example(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    value = str(value or "").strip()
    return value if _META_EXAMPLE_RE.fullmatch(value) else ""


def _dynamic_example(local_button):
    """Return one Meta-valid replacement value, never a completed URL."""

    if not isinstance(local_button, dict):
        return "sample"
    for key in ("url_example", "_meta_url_example", "example"):
        value = _first_example(local_button.get(key))
        if value:
            return value
    return "sample"


def install_template_cta_meta_compat():
    """Patch tracked CTA serialization after core validation is installed."""

    global _INSTALLED
    if _INSTALLED:
        return

    original_encode = tracking.encode_cta_token
    original_decode = tracking.decode_cta_token
    original_native_button = tracking._native_button
    original_tracked_button = tracking._tracked_meta_button

    def encode_cta_token(**kwargs):
        # Django's signed value contains colon separators. They are valid RFC
        # URL characters, but Meta's template validator has rejected such CTA
        # values in practice. A second URL-safe base64 envelope limits the path
        # segment to letters, digits, underscore and hyphen.
        return _compact_token(original_encode(**kwargs))

    def decode_cta_token(token):
        # Existing approved templates may still contain the original signed
        # token, so decoding remains backward-compatible.
        return original_decode(_expand_token(token))

    def native_button(button):
        result = original_native_button(button)
        if (
            result is not None
            and isinstance(button, dict)
            and str(button.get("type") or "").upper() == "URL"
        ):
            example = _first_example(button.get("example"))
            if example:
                result["url_example"] = example
        return result

    def tracked_meta_button(
        *,
        template,
        local_button,
        button_index,
        card_index=None,
    ):
        payload = original_tracked_button(
            template=template,
            local_button=local_button,
            button_index=button_index,
            card_index=card_index,
        )
        if not payload or str(payload.get("type") or "").upper() != "URL":
            return payload

        definition = tracking._definition(local_button)
        if definition and definition.get("placeholder"):
            # Meta expects ["summer2023"], not
            # ["https://host/path/summer2023"].
            payload["example"] = [_dynamic_example(local_button)]
        else:
            payload.pop("example", None)
        return payload

    tracking.encode_cta_token = encode_cta_token
    tracking.decode_cta_token = decode_cta_token
    tracking._native_button = native_button
    tracking._tracked_meta_button = tracked_meta_button
    _INSTALLED = True


def tracked_template_cta_compatible(request, *args, **kwargs):
    """Serve Meta URL-verification HEAD requests without recording a click."""

    from apps.channels.template_cta_public import tracked_template_cta

    if request.method != "HEAD":
        return tracked_template_cta(request, *args, **kwargs)

    original_method = request.method
    try:
        request.method = "GET"
        response = tracked_template_cta(request, *args, **kwargs)
    finally:
        request.method = original_method

    # A HEAD response has the same status and headers as GET but no entity body.
    response.content = b""
    response["Content-Length"] = "0"
    return response
