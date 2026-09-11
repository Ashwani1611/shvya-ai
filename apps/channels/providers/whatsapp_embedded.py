"""Meta Graph reads used only while completing WhatsApp Embedded Signup.

These calls run after SHVYA has exchanged Meta's short-lived authorization code
for a customer-scoped business token. They contain no CRM/database logic.
"""

import requests

from .whatsapp import GRAPH_API_BASE, REQUEST_TIMEOUT_SECONDS, WhatsAppAPIError


def _get_json(url, *, params=None, headers=None, error_label):
    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise WhatsAppAPIError(f"Network error {error_label}: {exc}") from exc

    if not response.ok:
        raise WhatsAppAPIError(
            f"Meta {error_label} returned {response.status_code}",
            status_code=response.status_code,
            response_body=response.text,
        )

    try:
        return response.json()
    except ValueError as exc:
        raise WhatsAppAPIError(
            f"Meta {error_label} returned invalid JSON.",
            status_code=response.status_code,
            response_body=response.text,
        ) from exc


def exchange_code_for_access_token(
    *,
    app_id,
    app_secret,
    code,
    redirect_uri="",
):
    """Exchange an Embedded Signup OAuth code for a BISU access token.

    Direct Facebook Login for Business OAuth requires the token exchange to use
    the exact same ``redirect_uri`` used by the authorization dialog. The older
    JS-SDK path does not expose that URI, so ``redirect_uri`` remains optional
    for backwards compatibility with already-issued JS-SDK codes.
    """
    params = {
        "client_id": app_id,
        "client_secret": app_secret,
        "code": code,
    }
    if redirect_uri:
        params["redirect_uri"] = redirect_uri

    payload = _get_json(
        f"{GRAPH_API_BASE}/oauth/access_token",
        params=params,
        error_label="token exchange",
    )
    access_token = payload.get("access_token")
    if not access_token:
        raise WhatsAppAPIError(
            "Meta token exchange succeeded but returned no access_token."
        )
    return access_token


def debug_access_token(*, app_id, app_secret, access_token):
    """Inspect the BISU token and return Meta's debug-token data object."""
    payload = _get_json(
        f"{GRAPH_API_BASE}/debug_token",
        params={
            "input_token": access_token,
            "access_token": f"{app_id}|{app_secret}",
        },
        error_label="token debug",
    )
    return payload.get("data") or {}


def list_assigned_wabas(*, user_id, access_token):
    """Return WhatsApp Business Accounts assigned to the token's business user."""
    payload = _get_json(
        f"{GRAPH_API_BASE}/{user_id}/assigned_whatsapp_business_accounts",
        params={
            "fields": "id,name",
            "limit": 100,
            "access_token": access_token,
        },
        error_label="assigned WhatsApp Business Account lookup",
    )
    return payload.get("data") or []


def list_waba_phone_numbers(*, waba_id, access_token):
    """Return phone numbers visible to the token for one WABA."""
    payload = _get_json(
        f"{GRAPH_API_BASE}/{waba_id}/phone_numbers",
        params={
            "fields": "id,display_phone_number,verified_name",
            "limit": 100,
            "access_token": access_token,
        },
        error_label="WABA phone-number lookup",
    )
    return payload.get("data") or []
