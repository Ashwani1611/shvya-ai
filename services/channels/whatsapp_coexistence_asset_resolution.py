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
import time

import requests

from apps.channels.providers import whatsapp as whatsapp_provider
from services.channels.embedded_signup_service import (
    EmbeddedSignupPhoneSelectionRequired,
)

logger = logging.getLogger(__name__)

_INSTALLED = False
_CLOUD_API_PLATFORM_TYPES = {"CLOUD_API", "SMB_CLOUD_API"}
_RESOLUTION_RETRY_DELAYS = (0.0, 0.75, 1.5, 3.0)


def _choice_key(choice):
    return (
        str(choice.get("waba_id") or "").strip(),
        str(choice.get("phone_number_id") or "").strip(),
    )


def _as_true(value):
    return value is True or str(value or "").strip().lower() == "true"


def _candidate_status_from_meta(*, phone_number_id, access_token):
    """Read Coexistence status without making one optional field fatal.

    Meta documents ``is_on_biz_app`` + ``platform_type`` for verifying a
    Coexistence number. In practice Graph can temporarily reject one field while
    the post-Finish asset transition is still propagating. A single combined
    request then returns no usable status at all, which makes a multi-phone WABA
    look ambiguous. Query the documented pair first and fall back to each field
    separately, merging whatever Meta currently exposes.
    """
    url = f"{whatsapp_provider.GRAPH_API_BASE}/{phone_number_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    merged = {}

    for fields in (
        "is_on_biz_app,platform_type",
        "is_on_biz_app",
        "platform_type",
    ):
        try:
            response = requests.get(
                url,
                headers=headers,
                params={"fields": fields},
                timeout=whatsapp_provider.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            logger.warning(
                "Coexistence candidate status lookup hit a network error: phone_number_id=%s fields=%s",
                phone_number_id,
                fields,
                exc_info=True,
            )
            continue

        if not response.ok:
            logger.info(
                "Coexistence candidate status field set unavailable: phone_number_id=%s fields=%s status=%s",
                phone_number_id,
                fields,
                response.status_code,
            )
            continue

        try:
            payload = response.json()
        except ValueError:
            logger.info(
                "Coexistence candidate status returned invalid JSON: phone_number_id=%s fields=%s",
                phone_number_id,
                fields,
            )
            continue

        if isinstance(payload, dict):
            if "is_on_biz_app" in payload:
                merged["is_on_biz_app"] = payload.get("is_on_biz_app")
            if "platform_type" in payload:
                merged["platform_type"] = payload.get("platform_type")

        if "is_on_biz_app" in merged and "platform_type" in merged:
            break

    return merged


def _select_unique_coexistence_choice(*, choices, access_token, status_getter):
    """Return one Meta-authorized Coexistence phone choice or ``None``.

    Selection is deliberately conservative:
    - prefer a unique ``is_on_biz_app=true`` + Cloud API phone;
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

        if not _as_true(status.get("is_on_biz_app")):
            continue

        on_biz_app.append(choice)
        platform_type = str(status.get("platform_type") or "").strip().upper()
        if platform_type in _CLOUD_API_PLATFORM_TYPES:
            cloud_api.append(choice)

    if len(cloud_api) == 1:
        return cloud_api[0]
    if len(on_biz_app) == 1:
        return on_biz_app[0]
    return None


def _resolve_choice_with_meta_propagation(
    *,
    choices,
    access_token,
    status_getter=_candidate_status_from_meta,
    retry_delays=_RESOLUTION_RETRY_DELAYS,
    sleep_fn=time.sleep,
):
    """Give Meta a short bounded window to expose the selected Coexistence phone.

    The OAuth redirect can reach SHVYA a fraction of a second before the selected
    phone's ``is_on_biz_app`` / ``platform_type`` state has converged. Retrying a
    few times avoids forcing the admin through Embedded Signup again after Meta
    has already shown a successful Finish screen.
    """
    delays = tuple(retry_delays or (0.0,))
    if not delays:
        delays = (0.0,)

    for index, delay in enumerate(delays):
        if index and delay:
            sleep_fn(delay)
        selected = _select_unique_coexistence_choice(
            choices=choices,
            access_token=access_token,
            status_getter=status_getter,
        )
        if selected is not None:
            return selected
    return None


def install_whatsapp_coexistence_asset_resolution():
    """Patch only Coexistence signup asset resolution.

    The normal shared resolver remains untouched for Connect API. If Meta still
    cannot distinguish a single Coexistence number after the short propagation
    window, the original selection exception is re-raised and the existing
    fail-safe customer error is used.
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
            selected = _resolve_choice_with_meta_propagation(
                choices=exc.choices,
                access_token=access_token,
            )
            if selected is None:
                raise

            selected_waba_id, selected_phone_number_id = _choice_key(selected)
            logger.info(
                "Resolved omitted Coexistence phone after Meta Finish: waba_id=%s phone_number_id=%s candidates=%s",
                selected_waba_id,
                selected_phone_number_id,
                len(exc.choices or []),
            )
            return selected_waba_id, selected_phone_number_id

    coexistence._resolve_signup_assets = resolve_coexistence_assets
    _INSTALLED = True
