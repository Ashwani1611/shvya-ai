import pytest
from django.conf import settings
from django.http import HttpResponse

from apps.channels import connection_ui
from services.channels import embedded_signup_service


def _grant_debug_token(*, waba_ids=None, user_id="business-user-1"):
    granular = [
        {
            "scope": "whatsapp_business_management",
            "target_ids": list(waba_ids or []),
        },
        {
            "scope": "whatsapp_business_messaging",
            "target_ids": list(waba_ids or []),
        },
    ]
    return {
        "user_id": user_id,
        "scopes": [
            "whatsapp_business_management",
            "whatsapp_business_messaging",
        ],
        "granular_scopes": granular,
    }


def test_resolve_signup_assets_from_debug_token(monkeypatch):
    monkeypatch.setattr(settings, "META_APP_ID", "app-1")
    monkeypatch.setattr(settings, "META_APP_SECRET", "secret-1")
    monkeypatch.setattr(
        embedded_signup_service.embedded_provider,
        "debug_access_token",
        lambda **kwargs: _grant_debug_token(waba_ids=["waba-1"]),
    )
    monkeypatch.setattr(
        embedded_signup_service.embedded_provider,
        "list_waba_phone_numbers",
        lambda **kwargs: [{"id": "phone-1"}],
    )

    assert embedded_signup_service._resolve_signup_assets(
        access_token="token-1",
    ) == ("waba-1", "phone-1")


def test_resolve_signup_assets_falls_back_to_assigned_wabas(monkeypatch):
    monkeypatch.setattr(settings, "META_APP_ID", "app-1")
    monkeypatch.setattr(settings, "META_APP_SECRET", "secret-1")
    monkeypatch.setattr(
        embedded_signup_service.embedded_provider,
        "debug_access_token",
        lambda **kwargs: _grant_debug_token(),
    )
    monkeypatch.setattr(
        embedded_signup_service.embedded_provider,
        "list_assigned_wabas",
        lambda **kwargs: [{"id": "waba-2", "name": "Acme"}],
    )
    monkeypatch.setattr(
        embedded_signup_service.embedded_provider,
        "list_waba_phone_numbers",
        lambda **kwargs: [{"id": "phone-2"}],
    )

    assert embedded_signup_service._resolve_signup_assets(
        access_token="token-1",
    ) == ("waba-2", "phone-2")


def test_resolve_signup_assets_rejects_missing_whatsapp_scope(monkeypatch):
    monkeypatch.setattr(settings, "META_APP_ID", "app-1")
    monkeypatch.setattr(settings, "META_APP_SECRET", "secret-1")
    monkeypatch.setattr(
        embedded_signup_service.embedded_provider,
        "debug_access_token",
        lambda **kwargs: {
            "scopes": ["whatsapp_business_management"],
            "granular_scopes": [],
        },
    )

    with pytest.raises(embedded_signup_service.EmbeddedSignupError) as exc_info:
        embedded_signup_service._resolve_signup_assets(access_token="token-1")

    assert exc_info.value.stage == "asset_discovery"
    assert "whatsapp_business_messaging" in str(exc_info.value)


def test_resolve_signup_assets_never_guesses_between_multiple_phones(monkeypatch):
    monkeypatch.setattr(
        embedded_signup_service.embedded_provider,
        "list_waba_phone_numbers",
        lambda **kwargs: [{"id": "phone-1"}, {"id": "phone-2"}],
    )

    with pytest.raises(embedded_signup_service.EmbeddedSignupError) as exc_info:
        embedded_signup_service._resolve_signup_assets(
            access_token="token-1",
            waba_id="waba-1",
        )

    assert exc_info.value.stage == "asset_discovery"
    assert "multiple phone numbers" in str(exc_info.value)


def test_connect_api_response_disables_fedcm_at_browser_policy_level():
    response = HttpResponse("ok")

    result = connection_ui._disable_fedcm_for_embedded_signup(response)

    assert result["Permissions-Policy"] == "identity-credentials-get=()"


def test_fedcm_policy_preserves_existing_permissions_policy():
    response = HttpResponse("ok")
    response["Permissions-Policy"] = "camera=(), microphone=()"

    result = connection_ui._disable_fedcm_for_embedded_signup(response)

    assert result["Permissions-Policy"] == (
        "camera=(), microphone=(), identity-credentials-get=()"
    )
