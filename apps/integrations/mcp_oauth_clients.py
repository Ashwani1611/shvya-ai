"""Shared OAuth client hardening for SHVYA MCP authorization servers."""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import requests


MAX_CIMD_BYTES = 32 * 1024
CIMD_TIMEOUT = (2.0, 5.0)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class MCPClientMetadataError(ValueError):
    pass


def _web_provider_host_allowed(host: str) -> bool:
    """Legacy/Diagnostic provider allowlist. Keep intentionally narrow."""

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


def _operations_cimd_host_allowed(host: str) -> bool:
    """Known MCP client publishers safe for server-side CIMD fetching."""

    host = str(host or "").lower()
    suffixes = (
        "chatgpt.com",
        "openai.com",
        "claude.ai",
        "anthropic.com",
        "vscode.dev",
        "cursor.com",
        "google.com",
        "googleapis.com",
        "windsurf.com",
        "codeium.com",
        "github.com",
    )
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _cimd_host_allowed(host: str) -> bool:
    host = str(host or "").lower()
    return bool(
        _web_provider_host_allowed(host)
        or host == "vscode.dev"
        or host.endswith(".vscode.dev")
    )


def _safe_parsed_uri(uri: str):
    try:
        parsed = urlparse(str(uri or ""))
        # Accessing .port validates malformed/out-of-range ports.
        _ = parsed.port
        return parsed
    except (TypeError, ValueError):
        return None


def _is_loopback_http_redirect(uri: str) -> bool:
    parsed = _safe_parsed_uri(uri)
    if parsed is None:
        return False
    host = (parsed.hostname or "").lower()
    return bool(
        parsed.scheme == "http"
        and host in LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def is_allowed_external_ai_redirect(uri: str) -> bool:
    """Legacy/Diagnostic callback policy. Do not broaden this implicitly."""

    parsed = _safe_parsed_uri(uri)
    if parsed is None:
        return False
    host = (parsed.hostname or "").lower()
    port = parsed.port

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


def is_allowed_operations_redirect(uri: str) -> bool:
    """Operations callback policy for standards-compliant remote MCP clients.

    Public web clients may dynamically register a normal HTTPS callback. Native
    clients may use RFC 8252 loopback HTTP callbacks on localhost/127.0.0.1/::1
    with a fixed or ephemeral port. PKCE S256, exact client binding, consent,
    issuer signalling, and exact authorization-code redirect binding remain
    mandatory elsewhere in the Operations OAuth flow.
    """

    parsed = _safe_parsed_uri(uri)
    if parsed is None:
        return False

    host = (parsed.hostname or "").lower()
    port = parsed.port
    if _is_loopback_http_redirect(uri):
        return True

    return bool(
        parsed.scheme == "https"
        and bool(host)
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _redirect_components(uri: str):
    parsed = _safe_parsed_uri(uri)
    if parsed is None:
        return None
    return (
        parsed.scheme,
        (parsed.hostname or "").lower(),
        parsed.port,
        parsed.path or "/",
        parsed.params,
        parsed.query,
    )


def operations_redirect_uri_matches_registered(
    registered_uris,
    requested_uri: str,
) -> bool:
    """Match Operations redirects, including RFC 8252 variable loopback ports.

    A registered loopback URI without an explicit port can match the same
    scheme/host/path/query at a runtime-selected port. Fixed-port registrations
    remain exact, and localhost/127.0.0.1/::1 are never treated as equivalent
    hosts.
    """

    requested_uri = str(requested_uri or "").strip()
    if not is_allowed_operations_redirect(requested_uri):
        return False
    requested = _redirect_components(requested_uri)
    if requested is None:
        return False

    for raw in registered_uris or []:
        registered_uri = str(raw or "").strip()
        if registered_uri == requested_uri:
            return True
        if not _is_loopback_http_redirect(registered_uri):
            continue
        registered = _redirect_components(registered_uri)
        if registered is None:
            continue
        (
            registered_scheme,
            registered_host,
            registered_port,
            registered_path,
            registered_params,
            registered_query,
        ) = registered
        (
            requested_scheme,
            requested_host,
            requested_port,
            requested_path,
            requested_params,
            requested_query,
        ) = requested
        if registered_port is not None:
            continue
        if (
            registered_scheme == requested_scheme == "http"
            and registered_host == requested_host
            and registered_host in LOOPBACK_HOSTS
            and requested_port is not None
            and registered_path == requested_path
            and registered_params == requested_params
            and registered_query == requested_query
        ):
            return True
    return False


def is_allowed_cimd_url(client_id: str) -> bool:
    """Legacy/Diagnostic CIMD policy."""

    parsed = _safe_parsed_uri(client_id)
    if parsed is None:
        return False

    host = (parsed.hostname or "").lower()
    port = parsed.port
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


def is_allowed_operations_cimd_url(client_id: str) -> bool:
    """Operations CIMD fetching stays publisher-allowlisted to avoid SSRF."""

    parsed = _safe_parsed_uri(client_id)
    if parsed is None:
        return False

    host = (parsed.hostname or "").lower()
    port = parsed.port
    path = unquote(parsed.path or "")
    segments = [segment for segment in path.split("/") if segment]
    return bool(
        parsed.scheme == "https"
        and _operations_cimd_host_allowed(host)
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


def _fetch_cimd_metadata(
    client_id: str,
    *,
    client_id_validator,
    redirect_validator,
) -> dict:
    client_id = str(client_id or "").strip()
    if len(client_id) > 255:
        raise MCPClientMetadataError(
            "Client ID Metadata Document URL is too long for this SHVYA OAuth client."
        )
    if not client_id_validator(client_id):
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
    if any(not redirect_validator(uri) for uri in redirect_uris):
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
    supported_auth_methods = data.get(
        "token_endpoint_auth_methods_supported"
    )
    if supported_auth_methods is not None:
        if (
            not isinstance(supported_auth_methods, list)
            or "none" not in supported_auth_methods
        ):
            raise MCPClientMetadataError(
                "SHVYA MCP requires public PKCE token authentication."
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


def fetch_cimd_metadata(client_id: str) -> dict:
    """Fetch CIMD metadata under the legacy/Diagnostic trust policy."""

    return _fetch_cimd_metadata(
        client_id,
        client_id_validator=is_allowed_cimd_url,
        redirect_validator=is_allowed_external_ai_redirect,
    )


def fetch_operations_cimd_metadata(client_id: str) -> dict:
    """Fetch CIMD metadata under the Operations multi-client trust policy."""

    return _fetch_cimd_metadata(
        client_id,
        client_id_validator=is_allowed_operations_cimd_url,
        redirect_validator=is_allowed_operations_redirect,
    )
