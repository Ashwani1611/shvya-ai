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

    def __init__(
        self,
        message,
        status_code=None,
        response_body=None,
    ):
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

        client.send_text_message(
            to="919876543210",
            body="Hello",
        )
    """

    def __init__(
        self,
        phone_number_id,
        access_token,
    ):
        self.phone_number_id = phone_number_id
        self.access_token = access_token

    # ============================================================
    # AUTHENTICATION
    # ============================================================

    def _headers(self):
        """
        JSON request headers for WhatsApp message endpoints.
        """
        return {
            "Authorization": (
                f"Bearer {self.access_token}"
            ),
            "Content-Type": "application/json",
        }

    # ============================================================
    # JSON POST
    # ============================================================

    def _post(
        self,
        path,
        payload,
    ):
        """
        Perform a JSON POST against Meta's Graph API.
        """
        url = (
            f"{GRAPH_API_BASE}/{path}"
        )

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
                (
                    "WhatsApp API returned "
                    f"{response.status_code}"
                ),
                status_code=response.status_code,
                response_body=response.text,
            )

        try:
            return response.json()

        except ValueError as exc:
            raise WhatsAppAPIError(
                "WhatsApp API returned invalid JSON.",
                status_code=response.status_code,
                response_body=response.text,
            ) from exc

    # ============================================================
    # TEXT MESSAGE
    # ============================================================

    def send_text_message(
        self,
        to,
        body,
        preview_url=False,
    ):
        """
        Send a free-form text message.

        NOTE:
        Meta only allows free-form customer-service messages
        within the applicable 24-hour customer-service window.

        Outside that window, an approved template is required.
        This provider client does not enforce that rule; the
        service layer is responsible for transport eligibility.
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

    # ============================================================
    # TEMPLATE MESSAGE
    # ============================================================

    def send_template_message(
        self,
        to,
        template_name,
        language_code="en_US",
        components=None,
    ):
        """
        Send a pre-approved WhatsApp template message.

        Used for outbound communication that requires a
        template rather than free-form text.
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": language_code,
                },
                "components": components or [],
            },
        }

        return self._post(
            f"{self.phone_number_id}/messages",
            payload,
        )

    # ============================================================
    # MEDIA UPLOAD
    # ============================================================

    def upload_media(
        self,
        *,
        file_obj,
        filename,
        mime_type,
    ):
        """
        Upload a local file to Meta's WhatsApp media endpoint.

        This method only handles the Meta HTTP request.

        It does NOT:
            - resolve a SHVYA Document
            - inspect a Lead
            - perform organization checks
            - make AI decisions
            - create database records
        """
        url = (
            f"{GRAPH_API_BASE}/"
            f"{self.phone_number_id}/media"
        )

        headers = {
            "Authorization": (
                f"Bearer {self.access_token}"
            ),
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                data={
                    "messaging_product": "whatsapp",
                },
                files={
                    "file": (
                        filename,
                        file_obj,
                        mime_type,
                    ),
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

        except requests.RequestException as exc:
            raise WhatsAppAPIError(
                (
                    "Network error uploading "
                    f"WhatsApp media: {exc}"
                )
            ) from exc

        if not response.ok:
            raise WhatsAppAPIError(
                (
                    "WhatsApp media upload returned "
                    f"{response.status_code}"
                ),
                status_code=response.status_code,
                response_body=response.text,
            )

        try:
            data = response.json()

        except ValueError as exc:
            raise WhatsAppAPIError(
                "WhatsApp media upload returned invalid JSON.",
                status_code=response.status_code,
                response_body=response.text,
            ) from exc

        media_id = data.get("id")

        if not media_id:
            raise WhatsAppAPIError(
                (
                    "WhatsApp media upload succeeded "
                    "but returned no media ID."
                ),
                status_code=response.status_code,
                response_body=response.text,
            )

        return data

    # ============================================================
    # MEDIA MESSAGE
    # ============================================================

    def send_media_message(
        self,
        *,
        to,
        media_type,
        media_id=None,
        media_url=None,
        caption=None,
        filename=None,
    ):
        """
        Send an image, audio, video, or document message.

        Media can be referenced either by:
            - Meta media_id
            - publicly accessible media_url

        Exactly one of media_id or media_url must be supplied.

        Supported media types:
            image
            audio
            video
            document
        """
        allowed_types = {
            "image",
            "audio",
            "video",
            "document",
        }

        if media_type not in allowed_types:
            raise ValueError(
                (
                    "Unsupported WhatsApp media type: "
                    f"{media_type}"
                )
            )

        if bool(media_id) == bool(media_url):
            raise ValueError(
                (
                    "Exactly one of media_id or "
                    "media_url must be supplied."
                )
            )

        media_payload = {}

        if media_id:
            media_payload["id"] = media_id

        else:
            media_payload["link"] = media_url

        # Meta supports captions for these media types.
        if (
            caption
            and media_type
            in {
                "image",
                "video",
                "document",
            }
        ):
            media_payload["caption"] = caption

        if (
            filename
            and media_type == "document"
        ):
            media_payload["filename"] = filename

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": media_type,
            media_type: media_payload,
        }

        return self._post(
            f"{self.phone_number_id}/messages",
            payload,
        )

    # ============================================================
    # MARK AS READ
    # ============================================================

    def mark_as_read(
        self,
        message_id,
    ):
        """
        Mark an inbound WhatsApp message as read.
        """
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
# methods because they run BEFORE a WhatsAppAccount exists.
#
# They use SHVYA's own application credentials
# (META_APP_ID / META_APP_SECRET), not a connected account's
# phone_number_id / access_token.


def exchange_code_for_access_token(
    app_id,
    app_secret,
    code,
):
    """
    Step 1 of embedded signup: trade the short-lived
    authorization code returned by the Facebook JS SDK for
    a long-lived Meta access token.

    This must happen server-side because app_secret must
    never reach the browser.
    """
    url = (
        f"{GRAPH_API_BASE}/oauth/access_token"
    )

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
            (
                "Network error exchanging "
                f"embedded signup code: {exc}"
            )
        ) from exc

    if not response.ok:
        raise WhatsAppAPIError(
            (
                "Meta token exchange returned "
                f"{response.status_code}"
            ),
            status_code=response.status_code,
            response_body=response.text,
        )

    try:
        data = response.json()

    except ValueError as exc:
        raise WhatsAppAPIError(
            "Meta token exchange returned invalid JSON.",
            status_code=response.status_code,
            response_body=response.text,
        ) from exc

    access_token = data.get(
        "access_token"
    )

    if not access_token:
        raise WhatsAppAPIError(
            (
                "Meta token exchange succeeded "
                "but returned no access_token."
            ),
            response_body=response.text,
        )

    return access_token


def get_phone_number_details(
    phone_number_id,
    access_token,
):
    """
    Fetch the human-facing details for a Meta phone number.

    Used to populate WhatsAppAccount without requiring the
    user to manually type the display number or verified name.
    """
    url = (
        f"{GRAPH_API_BASE}/{phone_number_id}"
    )

    try:
        response = requests.get(
            url,
            params={
                "fields": (
                    "display_phone_number,"
                    "verified_name"
                ),
                "access_token": access_token,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.RequestException as exc:
        raise WhatsAppAPIError(
            (
                "Network error fetching "
                f"phone number details: {exc}"
            )
        ) from exc

    if not response.ok:
        raise WhatsAppAPIError(
            (
                "Meta phone number lookup returned "
                f"{response.status_code}"
            ),
            status_code=response.status_code,
            response_body=response.text,
        )

    try:
        return response.json()

    except ValueError as exc:
        raise WhatsAppAPIError(
            (
                "Meta phone number lookup "
                "returned invalid JSON."
            ),
            status_code=response.status_code,
            response_body=response.text,
        ) from exc


def subscribe_app_to_waba(
    waba_id,
    access_token,
):
    """
    Subscribe SHVYA's app to the WhatsApp Business Account's
    webhooks.

    Without this subscription, inbound messages and delivery
    statuses will not reach the webhook.
    """
    url = (
        f"{GRAPH_API_BASE}/"
        f"{waba_id}/subscribed_apps"
    )

    try:
        response = requests.post(
            url,
            params={
                "access_token": access_token,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.RequestException as exc:
        raise WhatsAppAPIError(
            (
                "Network error subscribing "
                f"app to WABA: {exc}"
            )
        ) from exc

    if not response.ok:
        raise WhatsAppAPIError(
            (
                "Meta WABA subscription returned "
                f"{response.status_code}"
            ),
            status_code=response.status_code,
            response_body=response.text,
        )

    try:
        return response.json()

    except ValueError as exc:
        raise WhatsAppAPIError(
            (
                "Meta WABA subscription "
                "returned invalid JSON."
            ),
            status_code=response.status_code,
            response_body=response.text,
        ) from exc