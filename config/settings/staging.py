"""Staging settings for SHVYA AI.

Staging intentionally stays close to production: DEBUG remains disabled,
production-only apps are enabled, secure cookies/HSTS remain active, and all
runtime differences come from environment variables and isolated infrastructure.
"""

from decouple import config

from .prod import *  # noqa: F401,F403

APP_ENV = "staging"
DEBUG = False
OPERATIONS_PUBLIC_BASE_URL = "https://staging.shvya-ai.com"

# Keep staging public binaries isolated from production even when both
# environments use the same S3 bucket.
AWS_S3_PUBLIC_ASSET_PREFIX = str(
    config(
        "AWS_S3_PUBLIC_ASSET_PREFIX",
        default="staging/media/public-assets",
    )
    or "staging/media/public-assets"
).strip().strip("/")

ALLOWED_HOSTS = [
    host.strip()
    for host in config(
        "ALLOWED_HOSTS",
        default="staging.shvya-ai.com,web",
    ).split(",")
    if host.strip()
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in config(
        "CSRF_TRUSTED_ORIGINS",
        default="https://staging.shvya-ai.com",
    ).split(",")
    if origin.strip()
]

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True

# Environment-level safety controls available to outbound transports. Staging
# should still use dedicated test credentials/accounts rather than production
# Meta, payment, email, or other third-party credentials.
OUTBOUND_MESSAGING_ENABLED = config(
    "OUTBOUND_MESSAGING_ENABLED",
    default=False,
    cast=bool,
)
STAGING_ALLOWED_RECIPIENTS = {
    value.strip().lstrip("+")
    for value in config(
        "STAGING_ALLOWED_RECIPIENTS",
        default="",
    ).split(",")
    if value.strip()
}
