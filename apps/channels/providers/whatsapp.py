"""
Thin client for Meta's WhatsApp Business Platform (Cloud API).

This module ONLY talks to Meta's HTTP API. No business logic, no
database writes, no CRM/lead awareness -- that all belongs in
services.channels.whatsapp_service, per CLAUDE.md rule 2.
"""
import requests

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

REQUEST_TIMEOUT_SECONDS = 15


class WhatsAppAPIError(Exception):
    """Raised when Meta's API returns a non-2xx response."""

    def __init__(self, message, status_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class WhatsAppClient:
    """
    One instance per WhatsAppAccount (per-org credentials).

    Usage:
        client = WhatsAppClient(
            phone_number_id=account.phone_number_id,
            access_token=account.access_token,
        )
        client.send_text_message(to="919876543210", body="Hello")
    """

    def __init__(self, phone_number_id, access_token):
        self.phone_number_id = phone_number_id
        self.access_token = access_token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _post(self, path, payload):
        url = f"{GRAPH_API_BASE}/{path}"

        try:
            response = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

        except requests.RequestException as exc:
            raise WhatsAppAPIError(
                f"Network error calling WhatsApp API: {exc}"
            ) from exc

        if not response.ok:
            raise WhatsAppAPIError(
                f"WhatsApp API returned {response.status_code}",
                status_code=response.status_code,
                response_body=response.text,
            )

        return response.json()

    def send_text_message(self, to, body, preview_url=False):
        """
        Send a free-form text message.

        NOTE: Meta only allows free-form text within 24 hours of the
        customer's last inbound message. Outside that window, a
        pre-approved template message is required instead -- this
        client does not enforce that, the service layer does.
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {
                "body": body,
                "preview_url": preview_url,
            },
        }

        return self._post(
            f"{self.phone_number_id}/messages",
            payload,
        )

    def send_template_message(self, to, template_name, language_code="en_US", components=None):
        """
        Send a pre-approved template message (for outbound outside
        the 24-hour window, or the first message to a new contact).
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": components or [],
            },
        }

        return self._post(
            f"{self.phone_number_id}/messages",
            payload,
        )

    def mark_as_read(self, message_id):
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }

        return self._post(
            f"{self.phone_number_id}/messages",
            payload,
        )


# ============================================================
# EMBEDDED SIGNUP
# ============================================================
#
# These are module-level functions rather than WhatsAppClient
# methods because they run BEFORE a WhatsAppAccount exists --
# they use SHVYA's own app credentials (META_APP_ID/APP_SECRET),
# not a connected account's phone_number_id/access_token.


def exchange_code_for_access_token(app_id, app_secret, code):
    """
    Step 1 of embedded signup: trade the short-lived authorization
    `code` the Facebook JS SDK handed back in the browser for a
    long-lived Meta access token. Must happen server-side --
    app_secret can never reach the browser.
    """
    url = f"{GRAPH_API_BASE}/oauth/access_token"

    try:
        response = requests.get(
            url,
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.RequestException as exc:
        raise WhatsAppAPIError(
            f"Network error exchanging embedded signup code: {exc}"
        ) from exc

    if not response.ok:
        raise WhatsAppAPIError(
            f"Meta token exchange returned {response.status_code}",
            status_code=response.status_code,
            response_body=response.text,
        )

    data = response.json()
    access_token = data.get("access_token")

    if not access_token:
        raise WhatsAppAPIError(
            "Meta token exchange succeeded but returned no access_token.",
            response_body=response.text,
        )

    return access_token


def get_phone_number_details(phone_number_id, access_token):
    """
    Fetches the human-facing details (display number, verified
    business name) for a phone_number_id the embedded signup
    popup handed back -- used to populate WhatsAppAccount without
    asking the person to type them in.
    """
    url = f"{GRAPH_API_BASE}/{phone_number_id}"

    try:
        response = requests.get(
            url,
            params={
                "fields": "display_phone_number,verified_name",
                "access_token": access_token,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.RequestException as exc:
        raise WhatsAppAPIError(
            f"Network error fetching phone number details: {exc}"
        ) from exc

    if not response.ok:
        raise WhatsAppAPIError(
            f"Meta phone number lookup returned {response.status_code}",
            status_code=response.status_code,
            response_body=response.text,
        )

    return response.json()


def subscribe_app_to_waba(waba_id, access_token):
    """
    Subscribes SHVYA's app to this WhatsApp Business Account's
    webhooks (message/status callbacks). Embedded signup connects
    the number but does NOT do this automatically -- without it,
    inbound messages and delivery statuses never reach
    whatsapp_webhook_view.
    """
    url = f"{GRAPH_API_BASE}/{waba_id}/subscribed_apps"

    try:
        response = requests.post(
            url,
            params={"access_token": access_token},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.RequestException as exc:
        raise WhatsAppAPIError(
            f"Network error subscribing app to WABA: {exc}"
        ) from exc

    if not response.ok:
        raise WhatsAppAPIError(
            f"Meta WABA subscription returned {response.status_code}",
            status_code=response.status_code,
            response_body=response.text,
        )

    return response.json()