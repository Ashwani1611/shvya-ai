"""Bounded request-body transport helpers for SHVYA Operations MCP."""

from __future__ import annotations

from apps.integrations.diagnostic_auth import sanitize_text
from apps.integrations.operations.constants import (
    MAX_MCP_PUBLIC_REQUEST_BODY_BYTES,
    MAX_MCP_REQUEST_BODY_BYTES,
)
from apps.integrations.operations_auth import (
    OperationsAuthError,
    authenticate_bearer,
)


class OperationsMCPBodyError(Exception):
    status_code = 400
    code = -32600


class OperationsMCPBodyTooLarge(OperationsMCPBodyError):
    status_code = 413


class OperationsMCPLargeBodyAuthRequired(OperationsMCPBodyError):
    status_code = 401
    code = -32001

    def __init__(self, description):
        self.description = sanitize_text(description, limit=160)
        super().__init__("Authentication required for SHVYA Operations.")


def _content_length(request):
    raw = str(request.META.get("CONTENT_LENGTH") or "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise OperationsMCPBodyTooLarge("Request body is too large.") from exc


def _bearer(request):
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


def _require_large_body_auth(request):
    raw_bearer = _bearer(request)
    if not raw_bearer:
        raise OperationsMCPLargeBodyAuthRequired(
            "Authentication is required for large SHVYA Operations requests."
        )
    try:
        authenticate_bearer(raw_bearer)
    except OperationsAuthError as exc:
        raise OperationsMCPLargeBodyAuthRequired(str(exc)) from exc


def read_operations_mcp_body(request):
    content_length = _content_length(request)
    if content_length > MAX_MCP_REQUEST_BODY_BYTES:
        raise OperationsMCPBodyTooLarge("Request body is too large.")

    if content_length > MAX_MCP_PUBLIC_REQUEST_BODY_BYTES:
        _require_large_body_auth(request)

    raw_body = request.read(MAX_MCP_REQUEST_BODY_BYTES + 1)
    if len(raw_body) > MAX_MCP_REQUEST_BODY_BYTES:
        raise OperationsMCPBodyTooLarge("Request body is too large.")

    # Covers clients using a transfer mode without a Content-Length header.
    if len(raw_body) > MAX_MCP_PUBLIC_REQUEST_BODY_BYTES and (
        content_length <= MAX_MCP_PUBLIC_REQUEST_BODY_BYTES
    ):
        _require_large_body_auth(request)

    return raw_body
