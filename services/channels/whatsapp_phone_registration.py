"""Complete Meta Cloud API phone registration during Embedded Signup.

Meta's Embedded Signup authorization makes the WABA/phone assets available to
SHVYA, but the phone number still has to be registered with Cloud API before it
can receive WhatsApp traffic.  The existing connection flow fetched the phone
metadata, persisted the account and subscribed webhooks, but never called
``/{phone-number-id}/register``.  That left accounts looking Connected in SHVYA
while WhatsApp clients reported that the business number was not on WhatsApp.

Registration also enables Meta two-step verification and therefore requires a
6-digit PIN. SHVYA manages a stable, per-phone PIN derived from the server
SECRET_KEY. The PIN is never logged or persisted. SECRET_KEY is already the
credential-encryption root for WhatsApp access tokens in this application, so
rotating it already requires a credential migration.
"""

import hashlib
import hmac

import requests
from django.conf import settings

from apps.channels.providers import whatsapp as whatsapp_provider


_INSTALLED = False
_ORIGINAL_GET_PHONE_NUMBER_DETAILS = None


def _managed_registration_pin(phone_number_id):
    """Return a stable six-digit Meta registration PIN for one phone ID."""
    material = f"shvya-whatsapp-cloud-registration:{phone_number_id}".encode("utf-8")
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        material,
        hashlib.sha256,
    ).digest()
    # Keep the result in the human PIN range while remaining stable per phone.
    return str(100000 + (int.from_bytes(digest[:8], "big") % 900000))


def register_phone_number(*, phone_number_id, access_token):
    """Register a verified Embedded-Signup phone with Meta Cloud API."""
    url = f"{whatsapp_provider.GRAPH_API_BASE}/{phone_number_id}/register"
    payload = {
        "messaging_product": "whatsapp",
        "pin": _managed_registration_pin(phone_number_id),
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=whatsapp_provider.REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise whatsapp_provider.WhatsAppAPIError(
            f"Network error registering WhatsApp phone number: {exc}"
        ) from exc

    if not response.ok:
        raise whatsapp_provider.WhatsAppAPIError(
            f"Meta phone registration returned {response.status_code}",
            status_code=response.status_code,
            response_body=response.text,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise whatsapp_provider.WhatsAppAPIError(
            "Meta phone registration returned invalid JSON.",
            status_code=response.status_code,
            response_body=response.text,
        ) from exc

    if data.get("success") not in {True, "true", "True", 1}:
        raise whatsapp_provider.WhatsAppAPIError(
            "Meta phone registration did not confirm success.",
            status_code=response.status_code,
            response_body=response.text,
        )

    return data


def install_whatsapp_phone_registration():
    """Register phones immediately after Embedded Signup phone verification.

    Both Embedded Signup implementations in the repository call
    ``get_phone_number_details`` after OAuth/asset discovery and before saving
    the WhatsAppAccount. Wrapping that provider boundary puts registration in
    the correct lifecycle position without duplicating connection logic.
    """
    global _INSTALLED, _ORIGINAL_GET_PHONE_NUMBER_DETAILS

    if _INSTALLED:
        return

    _ORIGINAL_GET_PHONE_NUMBER_DETAILS = whatsapp_provider.get_phone_number_details

    def get_phone_number_details_and_register(phone_number_id, access_token):
        details = _ORIGINAL_GET_PHONE_NUMBER_DETAILS(
            phone_number_id=phone_number_id,
            access_token=access_token,
        )
        register_phone_number(
            phone_number_id=phone_number_id,
            access_token=access_token,
        )
        return details

    whatsapp_provider.get_phone_number_details = get_phone_number_details_and_register
    # Expose the transport explicitly for diagnostics/tests and future repair UI.
    whatsapp_provider.register_phone_number = register_phone_number
    _INSTALLED = True
