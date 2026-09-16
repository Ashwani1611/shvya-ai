from __future__ import annotations

import socket
from unittest.mock import Mock, patch

import pytest

from apps.ai_engagement.services import knowledge_url_security
from apps.ai_engagement.services.knowledge import (
    KnowledgeExtractionError,
    KnowledgeIngestionService,
)


class FakeResponse:
    def __init__(self, *, status=200, headers=None, chunks=None):
        self.status = status
        self._headers = {
            str(key).casefold(): value
            for key, value in (headers or {}).items()
        }
        self._chunks = list(chunks or [])
        self.closed = False

    def getheader(self, name, default=None):
        return self._headers.get(str(name).casefold(), default)

    def read(self, amount=None):
        del amount
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def public_dns(*args, **kwargs):
    del args, kwargs
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]


def test_loopback_ip_is_rejected_before_network_access():
    service = KnowledgeIngestionService()

    with patch(
        "apps.ai_engagement.services.knowledge_url_security._open_pinned_response"
    ) as open_response:
        with pytest.raises(
            KnowledgeExtractionError,
            match="public internet host",
        ):
            service.extract_url_text("http://127.0.0.1/admin")

    open_response.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/private",
        "http://172.16.10.2/private",
        "http://192.168.1.1/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/private",
        "http://[fc00::1]/private",
    ],
)
def test_non_public_ip_ranges_are_rejected(url):
    service = KnowledgeIngestionService()

    with pytest.raises(
        KnowledgeExtractionError,
        match="public internet host",
    ):
        service.normalize_url(url)


def test_hostname_resolving_to_private_address_is_rejected():
    service = KnowledgeIngestionService()

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.8", 443),
            )
        ],
    ):
        with pytest.raises(
            KnowledgeExtractionError,
            match="public internet host",
        ):
            service.normalize_url("https://internal.example.test/docs")


def test_mixed_public_and_private_dns_answers_fail_closed():
    service = KnowledgeIngestionService()

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("192.168.1.20", 443),
            ),
        ],
    ):
        with pytest.raises(
            KnowledgeExtractionError,
            match="public internet host",
        ):
            service.normalize_url("https://mixed.example.test/docs")


def test_non_default_ports_and_embedded_credentials_are_rejected():
    service = KnowledgeIngestionService()

    with pytest.raises(KnowledgeExtractionError, match="standard HTTP/HTTPS ports"):
        service.normalize_url("https://93.184.216.34:8443/docs")

    with pytest.raises(KnowledgeExtractionError, match="embedded credentials"):
        service.normalize_url("https://user:pass@93.184.216.34/docs")


def test_public_hostname_is_resolved_once_and_request_is_pinned_to_that_ip():
    service = KnowledgeIngestionService()
    connection = FakeConnection()
    response = FakeResponse(
        headers={"Content-Type": "text/html; charset=utf-8"},
        chunks=[b"<html><body>Hello</body></html>"],
    )

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        side_effect=public_dns,
    ) as resolver, patch(
        "apps.ai_engagement.services.knowledge_url_security._open_pinned_response",
        return_value=(connection, response),
    ) as open_response:
        text = service.extract_url_text("https://example.com/docs?q=1")

    assert text == "Hello"
    assert resolver.call_count == 1
    target = open_response.call_args.args[0]
    assert target.hostname == "example.com"
    assert target.connect_ip == "93.184.216.34"
    assert target.request_target == "/docs?q=1"
    assert response.closed is True
    assert connection.closed is True


def test_pinned_http_connection_connects_to_ip_not_hostname():
    connection = knowledge_url_security._PinnedHTTPConnection(
        hostname="example.com",
        connect_ip="93.184.216.34",
        port=80,
        timeout=20,
    )
    fake_socket = Mock()

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.create_connection",
        return_value=fake_socket,
    ) as create_connection:
        connection.connect()

    create_connection.assert_called_once_with(
        ("93.184.216.34", 80),
        20,
        None,
    )
    assert connection.sock is fake_socket


def test_pinned_https_uses_ip_for_tcp_and_hostname_for_tls_sni():
    connection = knowledge_url_security._PinnedHTTPSConnection(
        hostname="example.com",
        connect_ip="93.184.216.34",
        port=443,
        timeout=20,
    )
    raw_socket = Mock()
    wrapped_socket = Mock()
    context = Mock()
    context.wrap_socket.return_value = wrapped_socket
    connection._context = context

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.create_connection",
        return_value=raw_socket,
    ) as create_connection:
        connection.connect()

    create_connection.assert_called_once_with(
        ("93.184.216.34", 443),
        20,
        None,
    )
    context.wrap_socket.assert_called_once_with(
        raw_socket,
        server_hostname="example.com",
    )
    assert connection.sock is wrapped_socket


def test_redirect_to_private_destination_is_blocked_before_second_request():
    service = KnowledgeIngestionService()
    connection = FakeConnection()
    redirect = FakeResponse(
        status=302,
        headers={"Location": "http://127.0.0.1/private"},
    )

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        side_effect=public_dns,
    ), patch(
        "apps.ai_engagement.services.knowledge_url_security._open_pinned_response",
        return_value=(connection, redirect),
    ) as open_response:
        with pytest.raises(
            KnowledgeExtractionError,
            match="public internet host",
        ):
            service.extract_url_text("https://example.com/docs")

    assert open_response.call_count == 1
    assert redirect.closed is True
    assert connection.closed is True


def test_successful_public_html_fetch_keeps_existing_extraction_contract():
    service = KnowledgeIngestionService()
    connection = FakeConnection()
    response = FakeResponse(
        headers={"Content-Type": "text/html; charset=utf-8"},
        chunks=[b"<html><body><h1>Hello</h1><p>world</p></body></html>"],
    )

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        side_effect=public_dns,
    ), patch(
        "apps.ai_engagement.services.knowledge_url_security._open_pinned_response",
        return_value=(connection, response),
    ):
        text = service.extract_url_text("https://example.com/docs")

    assert text == "Hello\nworld"
    assert response.closed is True
    assert connection.closed is True


def test_declared_oversized_response_is_rejected():
    service = KnowledgeIngestionService()
    connection = FakeConnection()
    response = FakeResponse(
        headers={
            "Content-Type": "text/html",
            "Content-Length": str(
                knowledge_url_security.MAX_URL_RESPONSE_BYTES + 1
            ),
        },
        chunks=[b"not-read"],
    )

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        side_effect=public_dns,
    ), patch(
        "apps.ai_engagement.services.knowledge_url_security._open_pinned_response",
        return_value=(connection, response),
    ):
        with pytest.raises(
            KnowledgeExtractionError,
            match="response is too large",
        ):
            service.extract_url_text("https://example.com/large")

    assert response.closed is True
    assert connection.closed is True


def test_streamed_response_is_capped_even_without_content_length():
    service = KnowledgeIngestionService()
    connection = FakeConnection()
    response = FakeResponse(
        headers={"Content-Type": "text/html"},
        chunks=[b"12345", b"6789"],
    )

    with patch.object(
        knowledge_url_security,
        "MAX_URL_RESPONSE_BYTES",
        8,
    ), patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        side_effect=public_dns,
    ), patch(
        "apps.ai_engagement.services.knowledge_url_security._open_pinned_response",
        return_value=(connection, response),
    ):
        with pytest.raises(
            KnowledgeExtractionError,
            match="response is too large",
        ):
            service.extract_url_text("https://example.com/stream")

    assert response.closed is True
    assert connection.closed is True
