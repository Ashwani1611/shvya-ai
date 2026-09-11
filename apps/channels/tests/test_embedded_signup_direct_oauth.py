from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.conf import settings

from apps.channels import embedded_oauth_ui
from apps.channels.providers import whatsapp_embedded


def test_direct_oauth_url_uses_business_config_without_scope():
    url = embedded_oauth_ui._build_meta_oauth_url(
        app_id="app-123",
        config_id="config-456",
        redirect_uri="https://dashboard.example.com/wa/return/",
        state="state-789",
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "www.facebook.com"
    assert parsed.path == "/v25.0/dialog/oauth"
    assert query["client_id"] == ["app-123"]
    assert query["config_id"] == ["config-456"]
    assert query["redirect_uri"] == ["https://dashboard.example.com/wa/return/"]
    assert query["state"] == ["state-789"]
    assert query["response_type"] == ["code"]
    assert query["override_default_response_type"] == ["true"]
    assert query["auth_type"] == ["rerequest"]
    assert "scope" not in query


def test_direct_token_exchange_reuses_exact_redirect_uri(monkeypatch):
    captured = {}

    class Response:
        ok = True
        status_code = 200
        text = '{"access_token":"token-1"}'

        @staticmethod
        def json():
            return {"access_token": "token-1"}

    def fake_get(url, *, params, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(whatsapp_embedded.requests, "get", fake_get)

    token = whatsapp_embedded.exchange_code_for_access_token(
        app_id="app-123",
        app_secret="secret-456",
        code="code-789",
        redirect_uri="https://dashboard.example.com/wa/return/",
    )

    assert token == "token-1"
    assert captured["params"]["redirect_uri"] == (
        "https://dashboard.example.com/wa/return/"
    )
    assert captured["params"]["code"] == "code-789"


def test_direct_launcher_replaces_legacy_fb_login_click_handler():
    script = (
        Path(settings.BASE_DIR)
        / "static"
        / "js"
        / "whatsapp_embedded_signup_direct.js"
    ).read_text(encoding="utf-8")

    assert "cloneNode(true)" in script
    assert "direct/start/" in script
    assert "window.location.assign" in script
    assert "FB.login(" not in script
