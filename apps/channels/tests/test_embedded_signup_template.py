from pathlib import Path

from django.conf import settings


def _embedded_signup_template():
    return (
        Path(settings.BASE_DIR)
        / "templates"
        / "channels"
        / "whatsapp_connect_api.html"
    ).read_text(encoding="utf-8")


def test_embedded_signup_uses_facebook_login_for_business_v4_options():
    template = _embedded_signup_template()

    assert "config_id: '{{ meta_config_id }}'" in template
    assert "auth_type: 'rerequest'" in template
    assert "response_type: 'code'" in template
    assert "override_default_response_type: true" in template
    assert "extras: {setup: {}}" in template

    # Embedded Signup v4 gets products/assets/permissions from the Facebook
    # Login for Business configuration. Legacy v2/v3 overrides must not be sent.
    assert "sessionInfoVersion:" not in template
    assert "featureType: ''" not in template


def test_embedded_signup_keeps_fedcm_off_and_restricts_postmessage_origins():
    template = _embedded_signup_template()

    assert "fedCM: false" in template
    assert "origin === 'https://www.facebook.com'" in template
    assert "origin === 'https://web.facebook.com'" in template
    assert "event.origin.endsWith('facebook.com')" not in template
