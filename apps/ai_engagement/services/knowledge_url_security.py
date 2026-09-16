"""Security boundary for organization-supplied knowledge URLs.

Knowledge URL ingestion is intentionally allowed to fetch public web pages, but
it must never become a generic server-side HTTP client.  This module validates
every destination before network access, re-validates redirects, disables
ambient proxy settings, and bounds the response size.

The installer wraps the existing KnowledgeIngestionService at app startup so all
current callers inherit the protection without changing their public contract.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

from apps.ai_engagement.services import knowledge as knowledge_service


MAX_URL_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_URL_REDIRECTS = 5
_RESPONSE_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_INSTALLED = False


def _normalise_and_validate_url(url: str) -> str:
    """Return a normalized URL only when it targets the public internet."""

    normalized = str(url or "").strip()
    if not normalized:
        raise knowledge_service.KnowledgeExtractionError("URL cannot be empty.")

    parsed = urlparse(normalized)
    if not parsed.scheme:
        normalized = f"https://{normalized}"
        parsed = urlparse(normalized)

    if parsed.scheme not in {"http", "https"}:
        raise knowledge_service.KnowledgeExtractionError(
            "Only HTTP and HTTPS URLs are supported."
        )

    if parsed.username is not None or parsed.password is not None:
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URLs cannot contain embedded credentials."
        )

    hostname = str(parsed.hostname or "").strip().rstrip(".")
    if not hostname:
        raise knowledge_service.KnowledgeExtractionError("Invalid URL.")

    lowered_host = hostname.casefold()
    if lowered_host == "localhost" or lowered_host.endswith(".localhost"):
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URLs must target a public internet host."
        )

    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URL contains an invalid port."
        ) from exc

    expected_port = 443 if parsed.scheme == "https" else 80
    if explicit_port not in {None, expected_port}:
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URLs may only use the standard HTTP/HTTPS ports."
        )

    _require_public_dns(hostname, explicit_port or expected_port)
    return normalized


def _require_public_dns(hostname: str, port: int) -> None:
    """Fail closed unless every resolved address is globally routable."""

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = {literal}
    else:
        try:
            records = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise knowledge_service.KnowledgeExtractionError(
                "Knowledge URL host could not be resolved."
            ) from exc

        addresses = set()
        for record in records:
            try:
                addresses.add(ipaddress.ip_address(record[4][0]))
            except (IndexError, ValueError):
                continue

    if not addresses:
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URL host resolved to no usable address."
        )

    if any(not address.is_global for address in addresses):
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URLs must target a public internet host."
        )


def _read_bounded_response(response) -> bytes:
    """Read a streamed HTTP response without allowing unbounded memory use."""

    content_length = response.headers.get("Content-Length", "")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > MAX_URL_RESPONSE_BYTES:
            raise knowledge_service.KnowledgeExtractionError(
                "Knowledge URL response is too large."
            )

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=_RESPONSE_CHUNK_BYTES):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_URL_RESPONSE_BYTES:
            raise knowledge_service.KnowledgeExtractionError(
                "Knowledge URL response is too large."
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _secure_extract_url_text(self, url: str) -> str:
    """Fetch one public HTML page with SSRF and response-size protections."""

    current_url = _normalise_and_validate_url(url)
    session = requests.Session()
    # Never inherit HTTP(S)_PROXY / NO_PROXY from the process environment for
    # user-controlled fetches. The destination checks below remain authoritative.
    session.trust_env = False

    try:
        for redirect_count in range(MAX_URL_REDIRECTS + 1):
            current_url = _normalise_and_validate_url(current_url)

            try:
                response = session.get(
                    current_url,
                    headers={"User-Agent": self.USER_AGENT},
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise knowledge_service.KnowledgeExtractionError(
                    f"Unable to fetch URL {current_url}: {exc}"
                ) from exc

            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = str(response.headers.get("Location") or "").strip()
                    if not location:
                        raise knowledge_service.KnowledgeExtractionError(
                            "Knowledge URL redirect did not include a destination."
                        )
                    if redirect_count >= MAX_URL_REDIRECTS:
                        raise knowledge_service.KnowledgeExtractionError(
                            "Knowledge URL redirected too many times."
                        )
                    # The next loop validates the redirect target before any
                    # request is made to it.
                    current_url = urljoin(current_url, location)
                    continue

                try:
                    response.raise_for_status()
                except requests.RequestException as exc:
                    raise knowledge_service.KnowledgeExtractionError(
                        f"Unable to fetch URL {current_url}: {exc}"
                    ) from exc

                content_type = str(response.headers.get("Content-Type") or "").lower()
                if (
                    "text/html" not in content_type
                    and "application/xhtml+xml" not in content_type
                ):
                    raise knowledge_service.KnowledgeExtractionError(
                        "The URL did not return an HTML page."
                    )

                raw = _read_bounded_response(response)
            finally:
                response.close()

            try:
                html = raw.decode("utf-8", errors="replace")
                soup = knowledge_service.BeautifulSoup(html, "html.parser")
                self._remove_unwanted_html(soup)
                return self._clean_text(soup.get_text(separator="\n"))
            except knowledge_service.KnowledgeExtractionError:
                raise
            except Exception as exc:
                raise knowledge_service.KnowledgeExtractionError(
                    f"Unable to parse URL {current_url}: {exc}"
                ) from exc

        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URL redirected too many times."
        )
    finally:
        session.close()


def _secure_normalize_url(self, url: str) -> str:
    return _normalise_and_validate_url(url)


def install_knowledge_url_security() -> None:
    """Install the knowledge URL security boundary once per process."""

    global _INSTALLED
    if _INSTALLED:
        return

    service = knowledge_service.KnowledgeIngestionService
    service._normalize_url = _secure_normalize_url
    service.extract_url_text = _secure_extract_url_text
    _INSTALLED = True
