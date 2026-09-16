"""Security boundary for organization-supplied knowledge URLs.

Knowledge URL ingestion may fetch public web pages, but it must never become a
generic server-side HTTP client. Every request and redirect is structurally
validated, resolved to public IP addresses, and then connected directly to one
of those validated addresses so DNS cannot change between validation and the
network connection.

The installer wraps the existing KnowledgeIngestionService at app startup so all
current callers inherit the protection without changing their public contract.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from apps.ai_engagement.services import knowledge as knowledge_service


MAX_URL_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_URL_REDIRECTS = 5
_RESPONSE_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_INSTALLED = False


@dataclass(frozen=True)
class _ValidatedTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    connect_ip: str
    request_target: str
    host_header: str


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection whose TCP destination is a pre-validated IP address."""

    def __init__(self, *, hostname: str, connect_ip: str, port: int, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an IP while verifying the original hostname."""

    def __init__(self, *, hostname: str, connect_ip: str, port: int, timeout: float):
        context = ssl.create_default_context()
        try:
            context.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:  # pragma: no cover - platform dependent
            pass
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=context,
        )
        self._connect_ip = connect_ip

    def connect(self):
        raw_socket = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


def _ascii_hostname(hostname: str) -> str:
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URL contains an invalid hostname."
        ) from exc


def _resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve once and fail closed unless every answer is globally routable."""

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [literal]
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

        addresses = []
        for record in records:
            try:
                address = ipaddress.ip_address(record[4][0])
            except (IndexError, ValueError):
                continue
            if address not in addresses:
                addresses.append(address)

    if not addresses:
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URL host resolved to no usable address."
        )

    if any(not address.is_global for address in addresses):
        raise knowledge_service.KnowledgeExtractionError(
            "Knowledge URLs must target a public internet host."
        )

    return tuple(str(address) for address in addresses)


def _validated_target(url: str) -> _ValidatedTarget:
    """Validate a URL and bind it to the exact public IP we will connect to."""

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

    hostname = _ascii_hostname(hostname)
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
    port = explicit_port or expected_port

    addresses = _resolve_public_addresses(hostname, port)

    request_target = parsed.path or "/"
    if parsed.params:
        request_target += f";{parsed.params}"
    if parsed.query:
        request_target += f"?{parsed.query}"

    try:
        literal_host = ipaddress.ip_address(hostname)
    except ValueError:
        literal_host = None
    host_header = f"[{hostname}]" if literal_host and literal_host.version == 6 else hostname
    if explicit_port is not None:
        host_header = f"{host_header}:{port}"

    return _ValidatedTarget(
        url=normalized,
        scheme=parsed.scheme,
        hostname=hostname,
        port=port,
        connect_ip=addresses[0],
        request_target=request_target,
        host_header=host_header,
    )


def _read_bounded_response(response: http.client.HTTPResponse) -> bytes:
    """Read a response without allowing unbounded memory consumption."""

    content_length = response.getheader("Content-Length", "")
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
    while True:
        chunk = response.read(_RESPONSE_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_URL_RESPONSE_BYTES:
            raise knowledge_service.KnowledgeExtractionError(
                "Knowledge URL response is too large."
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _open_pinned_response(target: _ValidatedTarget, *, timeout: float, user_agent: str):
    """Open a request to the already-resolved public IP without another DNS lookup."""

    connection_class = (
        _PinnedHTTPSConnection
        if target.scheme == "https"
        else _PinnedHTTPConnection
    )
    connection = connection_class(
        hostname=target.hostname,
        connect_ip=target.connect_ip,
        port=target.port,
        timeout=timeout,
    )
    try:
        connection.request(
            "GET",
            target.request_target,
            headers={
                "Host": target.host_header,
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                # Bound what we read without needing to decompress attacker-
                # controlled transfer encodings in application memory.
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        return connection, response
    except Exception:
        connection.close()
        raise


def _secure_extract_url_text(self, url: str) -> str:
    """Fetch one public HTML page with SSRF, rebinding, and size protections."""

    current_url = str(url or "").strip()

    for redirect_count in range(MAX_URL_REDIRECTS + 1):
        target = _validated_target(current_url)
        connection = None
        response = None

        try:
            connection, response = _open_pinned_response(
                target,
                timeout=self.REQUEST_TIMEOUT_SECONDS,
                user_agent=self.USER_AGENT,
            )

            if response.status in _REDIRECT_STATUSES:
                location = str(response.getheader("Location", "") or "").strip()
                if not location:
                    raise knowledge_service.KnowledgeExtractionError(
                        "Knowledge URL redirect did not include a destination."
                    )
                if redirect_count >= MAX_URL_REDIRECTS:
                    raise knowledge_service.KnowledgeExtractionError(
                        "Knowledge URL redirected too many times."
                    )
                current_url = urljoin(target.url, location)
                continue

            if not 200 <= response.status < 300:
                raise knowledge_service.KnowledgeExtractionError(
                    f"Unable to fetch URL {target.url}: HTTP {response.status}."
                )

            content_type = str(response.getheader("Content-Type", "") or "").lower()
            if (
                "text/html" not in content_type
                and "application/xhtml+xml" not in content_type
            ):
                raise knowledge_service.KnowledgeExtractionError(
                    "The URL did not return an HTML page."
                )

            content_encoding = str(
                response.getheader("Content-Encoding", "") or ""
            ).strip().lower()
            if content_encoding not in {"", "identity"}:
                raise knowledge_service.KnowledgeExtractionError(
                    "The URL returned an unsupported content encoding."
                )

            raw = _read_bounded_response(response)

        except knowledge_service.KnowledgeExtractionError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise knowledge_service.KnowledgeExtractionError(
                f"Unable to fetch URL {target.url}: {exc}"
            ) from exc
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

        try:
            html = raw.decode("utf-8", errors="replace")
            soup = knowledge_service.BeautifulSoup(html, "html.parser")
            self._remove_unwanted_html(soup)
            return self._clean_text(soup.get_text(separator="\n"))
        except knowledge_service.KnowledgeExtractionError:
            raise
        except Exception as exc:
            raise knowledge_service.KnowledgeExtractionError(
                f"Unable to parse URL {target.url}: {exc}"
            ) from exc

    raise knowledge_service.KnowledgeExtractionError(
        "Knowledge URL redirected too many times."
    )


def _secure_normalize_url(self, url: str) -> str:
    return _validated_target(url).url


def install_knowledge_url_security() -> None:
    """Install the knowledge URL security boundary once per process."""

    global _INSTALLED
    if _INSTALLED:
        return

    service = knowledge_service.KnowledgeIngestionService
    service._normalize_url = _secure_normalize_url
    service.extract_url_text = _secure_extract_url_text
    _INSTALLED = True
