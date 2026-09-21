"""Shared OAuth client hardening for SHVYA MCP authorization servers."""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import requests


MAX_CIMD_BYTES = 32 * 1024
CIMD_TIMEOUT = (2.0, 5.0)


class MCPClientMetadataError(ValueError):
    pass


def _web_provider_host_allowed(host: str) -> bool:
    host = str(host or "").lower()
    return (
        host == "chatgpt.com"
        or host.endswith(".chatgpt.com")
        or host == "openai.com"
        or host.endswith(".openai.com")
        or host == "claude.ai"
        or host.endswith(".claude.ai")
        or host == "anthropic.com"
        or host.endswith(".anthropic.com")
    )


def _cimd_host_allowed(host: str) -> bool:
    host = str(host or "").lower()
    return bool(
        _web_provider_host_allowed(host)
        or host == "vscode.dev"
        or host.endswith(".vscode.dev")
    )


def is_allowed_external_ai_redirect(uri: str) -> bool:
    try:
        parsed = urlparse(str(uri or ""))
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return False

    if (
        parsed.scheme == "http"
        and host == "127.0.0.1"
        and port == 33418
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    ):
        return True

    if (
        parsed.scheme == "https"
        and host == "vscode.dev"
        and parsed.path == "/redirect"
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    ):
        return True

    return bool(
        parsed.scheme == "https"
        and _web_provider_host_allowed(host)
        and port in {None, 443}
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def is_allowed_cimd_url(client_id: str) -> bool:
    try:
        parsed = urlparse(str(client_id or ""))
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return False

    path = unquote(parsed.path or "")
    segments = [segment for segment in path.split("/") if segment]
    return bool(
        parsed.scheme == "https"
        and _cimd_host_allowed(host)
        and port in {None, 443}
        and path not in {"", "/"}
        and segments
        and all(segment not in {".", ".."} for segment in segments)
        and "\\" not in path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def fetch_cimd_metadata(client_id: str) -> dict:
    """Fetch and validate a trusted-provider Client ID Metadata Document."""

    client_id = str(client_id or "").strip()
    if not is_allowed_cimd_url(client_id):
        raise MCPClientMetadataError(
            "Client ID Metadata Document URL is not an approved External AI HTTPS URL."
        )

    try:
        response = requests.get(
            client_id,
            headers={"Accept": "application/json"},
            timeout=CIMD_TIMEOUT,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise MCPClientMetadataError(
                "Client ID Metadata Document could not be loaded."
            )
        declared_length = response.headers.get("Content-Length")
        if declared_length and int(declared_length) > MAX_CIMD_BYTES:
            raise MCPClientMetadataError(
                "Client ID Metadata Document is too large."
            )
        raw = response.content
        if len(raw) > MAX_CIMD_BYTES:
            raise MCPClientMetadataError(
                "Client ID Metadata Document is too large."
            )
        data = response.json()
    except MCPClientMetadataError:
        raise
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise MCPClientMetadataError(
            "Client ID Metadata Document could not be validated."
        ) from exc

    if not isinstance(data, dict):
        raise MCPClientMetadataError(
            "Client ID Metadata Document must be a JSON object."
        )
    if str(data.get("client_id") or "") != client_id:
        raise MCPClientMetadataError(
            "Client ID Metadata Document client_id must match its document URL."
        )

    client_name = str(data.get("client_name") or "").strip()
    if not client_name or len(client_name) > 200:
        raise MCPClientMetadataError(
            "Client ID Metadata Document requires a valid client_name."
        )

    redirects = data.get("redirect_uris")
    if not isinstance(redirects, list):
        raise MCPClientMetadataError(
            "Client ID Metadata Document requires redirect_uris."
        )
    redirect_uris = list(
        dict.fromkeys(
            str(item or "").strip()
            for item in redirects
            if str(item or "").strip()
        )
    )
    if not redirect_uris or len(redirect_uris) > 8:
        raise MCPClientMetadataError(
            "Client ID Metadata Document must contain 1-8 redirect URIs."
        )
    if any(len(uri) > 2048 for uri in redirect_uris):
        raise MCPClientMetadataError(
            "Client ID Metadata Document redirect URI is too long."
        )
    if any(not is_allowed_external_ai_redirect(uri) for uri in redirect_uris):
        raise MCPClientMetadataError(
            "Client ID Metadata Document contains an unapproved redirect URI."
        )

    grant_types = list(
        data.get("grant_types")
        or ["authorization_code", "refresh_token"]
    )
    response_types = list(
        data.get("response_types")
        or ["code"]
    )
    if not set(grant_types) <= {"authorization_code", "refresh_token"}:
        raise MCPClientMetadataError(
            "Client ID Metadata Document requests an unsupported grant type."
        )
    if "authorization_code" not in grant_types:
        raise MCPClientMetadataError(
            "Client ID Metadata Document must support authorization_code."
        )
    if set(response_types) != {"code"}:
        raise MCPClientMetadataError(
            "Client ID Metadata Document must use response_type=code."
        )
    if data.get("token_endpoint_auth_method", "none") != "none":
        raise MCPClientMetadataError(
            "SHVYA MCP accepts only public PKCE clients."
        )

    application_type = str(
        data.get("application_type") or "web"
    )
    if application_type not in {"web", "native"}:
        raise MCPClientMetadataError(
            "Client ID Metadata Document has an unsupported application_type."
        )

    return {
        "client_id": client_id,
        "client_name": client_name,
        "application_type": application_type,
        "redirect_uris": redirect_uris,
        "grant_types": grant_types,
        "response_types": response_types,
    }
