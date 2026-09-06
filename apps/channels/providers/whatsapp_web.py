"""Thin HTTP client for SHVYA's internal whatsapp-web.js gateway.

The gateway owns Chromium/WhatsApp Web sessions. This module deliberately
contains no CRM, tenant, lead, or database logic. Hosted-account business
rules live in services.channels.hosted_whatsapp_service.
"""

import requests
from decouple import config

from apps.channels.providers.whatsapp import WhatsAppAPIError


REQUEST_TIMEOUT_SECONDS = 20


class WhatsAppWebGatewayError(WhatsAppAPIError):
    """Raised when the internal WhatsApp Web gateway cannot complete a call."""


class WhatsAppWebClient:
    def __init__(self):
        self.base_url = config(
            "WHATSAPP_WEB_GATEWAY_URL",
            default="http://whatsapp-web-gateway:3000",
        ).rstrip("/")
        self.token = config("WHATSAPP_WEB_GATEWAY_TOKEN", default="")

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method, path, *, payload=None, timeout=REQUEST_TIMEOUT_SECONDS):
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise WhatsAppWebGatewayError(
                f"WhatsApp Web gateway network error: {exc}",
                status_code=None,
            ) from exc

        if not response.ok:
            try:
                body = response.json()
                detail = body.get("error") or body.get("detail") or response.text
            except ValueError:
                detail = response.text
            raise WhatsAppWebGatewayError(
                f"WhatsApp Web gateway returned {response.status_code}: {detail}",
                status_code=response.status_code,
                response_body=response.text,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise WhatsAppWebGatewayError(
                "WhatsApp Web gateway returned invalid JSON.",
                status_code=response.status_code,
                response_body=response.text,
            ) from exc

    def create_session(self, *, session_id, phone_number):
        return self._request(
            "POST",
            "/sessions",
            payload={
                "sessionId": str(session_id),
                "phoneNumber": phone_number,
            },
            timeout=30,
        )

    def get_session(self, *, session_id):
        return self._request("GET", f"/sessions/{session_id}")

    def get_qr(self, *, session_id):
        return self._request("GET", f"/sessions/{session_id}/qr")

    def refresh_qr(self, *, session_id):
        return self._request("POST", f"/sessions/{session_id}/refresh-qr")

    def send_message(
        self,
        *,
        session_id,
        to_number,
        body,
        message_type="text",
        media_url=None,
        filename=None,
    ):
        return self._request(
            "POST",
            f"/sessions/{session_id}/messages",
            payload={
                "to": to_number,
                "body": body or "",
                "messageType": message_type,
                "mediaUrl": media_url,
                "filename": filename,
            },
            timeout=45,
        )

    def logout(self, *, session_id):
        return self._request("DELETE", f"/sessions/{session_id}", timeout=30)
