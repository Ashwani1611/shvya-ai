from __future__ import annotations

import socket
from unittest.mock import patch

import pytest
import requests

from apps.ai_engagement.services import knowledge_url_security
from apps.ai_engagement.services.knowledge import (
    KnowledgeExtractionError,
    KnowledgeIngestionService,
)


class FakeResponse:
    def __init__(self, *, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks or [])
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        yield from self._chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

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
        "apps.ai_engagement.services.knowledge_url_security.requests.Session.get"
    ) as request_get:
        with pytest.raises(
            KnowledgeExtractionError,
            match="public internet host",
        ):
            service.extract_url_text("http://127.0.0.1/admin")

    request_get.assert_not_called()


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


def test_redirect_to_private_destination_is_blocked_before_second_request():
    service = KnowledgeIngestionService()
    redirect = FakeResponse(
        status_code=302,
        headers={"Location": "http://127.0.0.1/private"},
    )

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        side_effect=public_dns,
    ), patch(
        "apps.ai_engagement.services.knowledge_url_security.requests.Session.get",
        autospec=True,
        return_value=redirect,
    ) as request_get:
        with pytest.raises(
            KnowledgeExtractionError,
            match="public internet host",
        ):
            service.extract_url_text("https://example.com/docs")

    assert request_get.call_count == 1
    assert redirect.closed is True


def test_successful_public_html_fetch_keeps_existing_extraction_contract():
    service = KnowledgeIngestionService()
    response = FakeResponse(
        headers={"Content-Type": "text/html; charset=utf-8"},
        chunks=[b"<html><body><h1>Hello</h1><p>world</p></body></html>"],
    )

    with patch(
        "apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo",
        side_effect=public_dns,
    ), patch(
        "apps.ai_engagement.services.knowledge_url_security.requests.Session.get",
        autospec=True,
        return_value=response,
    ) as request_get:
        text = service.extract_url_text("https://example.com/docs")

    assert text == "Hello\nworld"
    assert response.closed is True
    session = request_get.call_args.args[0]
    assert session.trust_env is False
    assert request_get.call_args.kwargs["allow_redirects"] is False
    assert request_get.call_args.kwargs["stream"] is True


def test_declared_oversized_response_is_rejected():
    service = KnowledgeIngestionService()
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
        "apps.ai_engagement.services.knowledge_url_security.requests.Session.get",
        autospec=True,
        return_value=response,
    ):
        with pytest.raises(
            KnowledgeExtractionError,
            match="response is too large",
        ):
            service.extract_url_text("https://example.com/large")

    assert response.closed is True


def test_streamed_response_is_capped_even_without_content_length():
    service = KnowledgeIngestionService()
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
        "apps.ai_engagement.services.knowledge_url_security.requests.Session.get",
        autospec=True,
        return_value=response,
    ):
        with pytest.raises(
            KnowledgeExtractionError,
            match="response is too large",
        ):
            service.extract_url_text("https://example.com/stream")

    assert response.closed is True
