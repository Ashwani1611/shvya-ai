"""Runtime hardening for Instagram Login credentials and authorization URLs.

Instagram API with Instagram Login uses the Instagram App ID / App Secret from
Meta's Instagram API setup. Those identifiers are distinct from credentials a
SHVYA deployment may already use for WhatsApp Embedded Signup.

The underlying Instagram service keeps backwards-compatible helpers for local
and test environments. Production enables
``META_INSTAGRAM_REQUIRE_DEDICATED_CREDENTIALS`` and this installer replaces the
credential/authorize helpers so a WhatsApp Meta App ID can never be sent to the
Instagram OAuth endpoint by accident.
"""

from urllib.parse import urlencode

from django.conf import settings
from django.core import checks


def _dedicated_credentials_required() -> bool:
    return bool(
        getattr(settings, "META_INSTAGRAM_REQUIRE_DEDICATED_CREDENTIALS", False)
    )


def _instagram_app_id() -> str:
    dedicated = str(getattr(settings, "META_INSTAGRAM_APP_ID", "") or "").strip()
    if dedicated or _dedicated_credentials_required():
        return dedicated
    return str(getattr(settings, "META_APP_ID", "") or "").strip()


def _instagram_app_secret() -> str:
    dedicated = str(
        getattr(settings, "META_INSTAGRAM_APP_SECRET", "") or ""
    ).strip()
    if dedicated or _dedicated_credentials_required():
        return dedicated
    return str(getattr(settings, "META_APP_SECRET", "") or "").strip()


def _meta_credentials_available() -> bool:
    return bool(_instagram_app_id() and _instagram_app_secret())


def _build_authorize_url(*, app_id: str, redirect_uri: str, state: str) -> str:
    """Build Meta's current Instagram professional-account login URL."""
    from services.channels.instagram_service import (
        INSTAGRAM_SCOPES,
        OAUTH_AUTHORIZE_URL,
    )

    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(INSTAGRAM_SCOPES),
            "state": state,
            # Current Instagram business-login setup uses force_reauth. Keep
            # Facebook login disabled because this integration intentionally
            # uses Instagram Login, not the Facebook Page-linked flow.
            "force_reauth": "true",
            "enable_fb_login": "0",
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{query}"


@checks.register("instagram")
def _instagram_runtime_check(app_configs, **kwargs):
    del app_configs, kwargs
    if not _dedicated_credentials_required() or _meta_credentials_available():
        return []
    return [
        checks.Warning(
            "Instagram Login is disabled because dedicated Instagram App credentials are missing.",
            hint=(
                "Set META_INSTAGRAM_APP_ID and META_INSTAGRAM_APP_SECRET from "
                "Meta App Dashboard > Instagram API setup."
            ),
            id="channels.W002",
        )
    ]


def install_instagram_runtime() -> None:
    """Install production-safe credential resolution into the service module."""
    from services.channels import instagram_service

    instagram_service.instagram_app_id = _instagram_app_id
    instagram_service.instagram_app_secret = _instagram_app_secret
    instagram_service.meta_credentials_available = _meta_credentials_available
    instagram_service.build_authorize_url = _build_authorize_url
