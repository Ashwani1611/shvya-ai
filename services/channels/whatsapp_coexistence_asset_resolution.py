"""Coexistence-only recovery when Meta omits ``phone_number_id``.

Meta's WhatsApp Business App onboarding completion event can contain only the
WABA ID. The shared Embedded Signup resolver correctly refuses to guess when a
WABA exposes more than one phone number. For Coexistence we have an additional,
authoritative discriminator: ``is_on_biz_app`` on each phone-number asset.

This module wraps only the resolver imported by
``whatsapp_coexistence_service``. Standard Connect API Embedded Signup keeps its
existing explicit phone-selection behavior.
"""

from __future__ import annotations

import logging

from services.channels.embedded_signup_service import (
    EmbeddedSignupPhoneSelectionRequired,
)

logger = logging.getLogger(__name__)

_INSTALLED = False


def _choice_key(choice):
    return (
        str(choice.get("waba_id") or "").strip(),
        str(choice.get("phone_number_id") or "").strip(),
    )


def _select_unique_coexistence_choice(*, choices, access_token, status_getter):
    """Return one Meta-authorized Coexistence phone choice or ``None``.

    Selection is deliberately conservative:
    - prefer a unique ``is_on_biz_app=true`` + ``platform_type=CLOUD_API`` phone;
    - otherwise accept a unique ``is_on_biz_app=true`` phone, since Meta can lag
      the platform transition immediately after Embedded Signup completion;
    - never pick the first item or guess when multiple candidates still match.
    """
    on_biz_app = []
    cloud_api = []
    seen = set()

    for choice in choices or []:
        if not isinstance(choice, dict):
            continue
        waba_id, phone_number_id = _choice_key(choice)
        if not waba_id or not phone_number_id:
            continue
        pair = (waba_id, phone_number_id)
        if pair in seen:
            continue
        seen.add(pair)

        try:
            status = status_getter(
                phone_number_id=phone_number_id,
                access_token=access_token,
            ) or {}
        except Exception:
            logger.warning(
                "Coexistence candidate status lookup failed: waba_id=%s phone_number_id=%s",
                waba_id,
                phone_number_id,
                exc_info=True,
            )
            continue

        if status.get("is_on_biz_app") is not True:
            continue

        on_biz_app.append(choice)
        if str(status.get("platform_type") or "").strip().upper() == "CLOUD_API":
            cloud_api.append(choice)

    if len(cloud_api) == 1:
        return cloud_api[0]
    if len(on_biz_app) == 1:
        return on_biz_app[0]
    return None


def install_whatsapp_coexistence_asset_resolution():
    """Patch only Coexistence signup asset resolution.

    The normal shared resolver remains untouched for Connect API. If Meta still
    cannot distinguish a single Coexistence number, the original selection
    exception is re-raised and the existing fail-safe customer error is used.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from services.channels import whatsapp_coexistence_service as coexistence

    original_resolver = coexistence._resolve_signup_assets

    def resolve_coexistence_assets(*, access_token, waba_id="", phone_number_id=""):
        try:
            return original_resolver(
                access_token=access_token,
                waba_id=waba_id,
                phone_number_id=phone_number_id,
            )
        except EmbeddedSignupPhoneSelectionRequired as exc:
            selected = _select_unique_coexistence_choice(
                choices=exc.choices,
                access_token=access_token,
                status_getter=coexistence._coexistence_status,
            )
            if selected is None:
                raise

            selected_waba_id, selected_phone_number_id = _choice_key(selected)
            logger.info(
                "Resolved omitted Coexistence phone from Meta Business App status: waba_id=%s phone_number_id=%s candidates=%s",
                selected_waba_id,
                selected_phone_number_id,
                len(exc.choices or []),
            )
            return selected_waba_id, selected_phone_number_id

    coexistence._resolve_signup_assets = resolve_coexistence_assets
    _INSTALLED = True
