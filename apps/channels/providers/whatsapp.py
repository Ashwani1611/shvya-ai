"""
Thin client for Meta's WhatsApp Business Platform (Cloud API).

This module ONLY talks to Meta's HTTP API. No business logic, no
database writes, no CRM/lead awareness -- that all belongs in
services.channels.whatsapp_service, per CLAUDE.md rule 2.
"""

import tempfile
import time
from urllib.parse import urlsplit

import requests


GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

REQUEST_TIMEOUT_SECONDS = 15
MAX_INBOUND_MEDIA_BYTES = 100 * 1024 * 1024
_MEDIA_DOWNLOAD_HOSTS = {"lookaside.fbsbx.com", "lookaside.facebook.com", "graph.facebook.com"}


def _retry_after_seconds(response):
    """Return a bounded Retry-After delay when the provider supplies one."""
    raw = str((getattr(response, "headers", {}) or {}).get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return max(1, min(value, 900))


class WhatsAppAPIError(Exception):
    """Raised when Meta's API returns a non-2xx response."""

    def __init__(
        self,
        message,
        status_code=None,
        response_body=None,
        retry_after=None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.retry_after = retry_after


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

    def download_media(self, media_id, *, max_bytes=MAX_INBOUND_MEDIA_BYTES):
        """Retrieve a fresh, phone-bound Meta URL and return bounded binary media.

        Credentials and the temporary URL remain server-side. Redirects are
        rejected so credentials cannot be forwarded to another origin. The
        caller owns/closes the returned file (normally via FileResponse).
        """
        if (
            not isinstance(media_id, str) or not media_id.isascii()
            or not media_id.isdigit() or len(media_id) > 64
            or not self.phone_number_id or not self.access_token
        ):
            raise WhatsAppAPIError("Media is unavailable.")
        max_bytes = min(max_bytes, MAX_INBOUND_MEDIA_BYTES)
        headers = {"Authorization": f"Bearer {self.access_token}"}
        metadata_response = None
        media_response = None
        file_obj = None
        started_at = time.monotonic()
        try:
            metadata_response = requests.get(
                f"{GRAPH_API_BASE}/{media_id}", headers=headers,
                params={"phone_number_id": self.phone_number_id},
                timeout=(5, REQUEST_TIMEOUT_SECONDS), allow_redirects=False,
            )
            if not 200 <= metadata_response.status_code < 300:
                raise WhatsAppAPIError("Media is unavailable.", status_code=metadata_response.status_code)
            metadata = metadata_response.json()
            if not isinstance(metadata, dict):
                raise WhatsAppAPIError("Media is unavailable.")
            url = metadata.get("url")
            if not isinstance(url, str) or len(url) > 8192:
                raise WhatsAppAPIError("Media is unavailable.")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https" or parsed.hostname not in _MEDIA_DOWNLOAD_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment
            ):
                raise WhatsAppAPIError("Media is unavailable.")
            file_size = metadata.get("file_size")
            if isinstance(file_size, int) and file_size > max_bytes:
                raise WhatsAppAPIError("Media exceeds the download size limit.", status_code=413)
            media_response = requests.get(
                url, headers=headers, stream=True,
                timeout=(5, REQUEST_TIMEOUT_SECONDS), allow_redirects=False,
            )
            if not 200 <= media_response.status_code < 300:
                raise WhatsAppAPIError("Media is unavailable.", status_code=media_response.status_code)
            content_length = str(media_response.headers.get("Content-Length") or "")
            if content_length.isdigit() and (len(content_length) > 10 or int(content_length) > max_bytes):
                raise WhatsAppAPIError("Media exceeds the download size limit.", status_code=413)
            file_obj = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b")
            size = 0
            for chunk in media_response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() - started_at > 30:
                    raise WhatsAppAPIError("Media could not be downloaded.")
                size += len(chunk)
                if size > max_bytes:
                    raise WhatsAppAPIError("Media exceeds the download size limit.", status_code=413)
                file_obj.write(chunk)
            if not size:
                raise WhatsAppAPIError("Media is unavailable.")
            file_obj.seek(0)
            return {"file": file_obj, "mime_type": metadata.get("mime_type", "")}
        except (requests.RequestException, ValueError) as exc:
            if file_obj is not None:
                file_obj.close()
            # Never attach a signed URL, token, raw provider body or exception.
            raise WhatsAppAPIError("Media could not be downloaded.") from exc
        except Exception:
            if file_obj is not None:
                file_obj.close()
            raise
        finally:
            if metadata_response is not None:
                metadata_response.close()
            if media_response is not None:
                media_response.close()

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
                retry_after=_retry_after_seconds(response),
            )

        try:
            return response.json()

        except ValueError as exc:
            raise WhatsAppAPIError(
                "WhatsApp API returned invalid JSON.",
                status_code=response.status_code,
                response_body=response.text,
                retry_after=_retry_after_seconds(response),
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
                retry_after=_retry_after_seconds(response),
            )

        try:
            data = response.json()

        except ValueError as exc:
            raise WhatsAppAPIError(
                "WhatsApp media upload returned invalid JSON.",
                status_code=response.status_code,
                response_body=response.text,
                retry_after=_retry_after_seconds(response),
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
                retry_after=_retry_after_seconds(response),
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
