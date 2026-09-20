from urllib.parse import urlsplit, urlunsplit

from decouple import config
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa


APP_ENV = "production"

# Production security is explicit and must not inherit DEBUG-dependent values
# calculated in base.py. Even if the deployment environment accidentally sets
# DEBUG=True, production remains fail-closed instead of disabling HTTPS,
# secure cookies, HSTS, or broadening CORS.
DEBUG = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
CORS_ALLOW_ALL_ORIGINS = False

# Production fails closed unless signing and recoverable-provider credentials
# use secrets distinct from Django's SECRET_KEY. Deploy workflows provision
# these values server-side before application containers start.
JWT_SECRET = str(config("JWT_SECRET", default="") or "").strip()
CREDENTIAL_ENCRYPTION_KEY = str(
    config("CREDENTIAL_ENCRYPTION_KEY", default="") or ""
).strip()
if not JWT_SECRET:
    raise ImproperlyConfigured("JWT_SECRET is required in production.")
if not CREDENTIAL_ENCRYPTION_KEY:
    raise ImproperlyConfigured(
        "CREDENTIAL_ENCRYPTION_KEY is required in production."
    )
if JWT_SECRET == SECRET_KEY:
    raise ImproperlyConfigured(
        "JWT_SECRET must be distinct from SECRET_KEY in production."
    )
if CREDENTIAL_ENCRYPTION_KEY == SECRET_KEY:
    raise ImproperlyConfigured(
        "CREDENTIAL_ENCRYPTION_KEY must be distinct from SECRET_KEY in production."
    )

SIMPLE_JWT = {
    **SIMPLE_JWT,
    "SIGNING_KEY": JWT_SECRET,
}

INSTALLED_APPS = [
    *INSTALLED_APPS,
    "django.contrib.postgres",
    "apps.hosted_automation",
]


def _redis_db_url(base_url, db_index):
    """Reuse the configured Redis server while isolating logical keyspaces."""
    parsed = urlsplit(str(base_url or ""))
    if parsed.scheme not in {"redis", "rediss"} or not parsed.netloc:
        raise ValueError(
            "REDIS_URL must be a redis:// or rediss:// URL when production "
            "Redis DB URLs are derived automatically."
        )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{int(db_index)}",
            parsed.query,
            parsed.fragment,
        )
    )


def _isolated_celery_url(setting_name, db_index):
    """Migrate legacy shared-/0 Celery URLs while preserving real overrides."""
    configured = str(config(setting_name, default="") or "").strip()
    base_url = str(REDIS_URL or "").strip()

    # Older SHVYA deployments commonly set CELERY_* to exactly REDIS_URL (/0).
    # Treat that as the legacy shared-keyspace configuration and migrate it to
    # the dedicated DB automatically. A genuinely different endpoint/backend
    # remains an explicit operator override and is preserved.
    if configured and configured != base_url:
        return configured
    return _redis_db_url(base_url, db_index)


# Keep independent Redis logical databases for unrelated runtime concerns.
# This prevents cache flushes or key maintenance from touching Celery broker
# state, result metadata, or Channels pub/sub keys. Cache/Channels URLs remain
# directly overrideable. Celery also preserves genuinely distinct legacy
# overrides while automatically migrating the old shared REDIS_URL value.
CACHE_REDIS_URL = config(
    "CACHE_REDIS_URL",
    default=_redis_db_url(REDIS_URL, 0),
)
CHANNEL_LAYER_REDIS_URL = config(
    "CHANNEL_LAYER_REDIS_URL",
    default=_redis_db_url(REDIS_URL, 1),
)
CELERY_BROKER_URL = _isolated_celery_url("CELERY_BROKER_URL", 2)
CELERY_RESULT_BACKEND = _isolated_celery_url("CELERY_RESULT_BACKEND", 3)

CACHES = {
    **CACHES,
    "default": {
        **CACHES["default"],
        "LOCATION": CACHE_REDIS_URL,
    },
}

CHANNEL_LAYERS = {
    **CHANNEL_LAYERS,
    "default": {
        **CHANNEL_LAYERS["default"],
        "CONFIG": {
            **CHANNEL_LAYERS["default"]["CONFIG"],
            "hosts": [CHANNEL_LAYER_REDIS_URL],
        },
    },
}

# The whatsapp-web.js gateway fetches short-lived signed follow-up media and
# posts authenticated session/message callbacks to Gunicorn over the private
# Docker network. Permit only the Docker service host in addition to the
# public hosts already supplied by the environment.
if "web" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, "web"]

# These internal gateway routes intentionally stay HTTP inside the private
# Docker network. The event endpoint is protected by WHATSAPP_WEB_CALLBACK_TOKEN
# and the media endpoint uses signed URLs. Public browser traffic is still
# forced to HTTPS by SecurityMiddleware.
SECURE_REDIRECT_EXEMPT = [
    r"^dashboard/whatsapp/connect/hosted/events/$",
    r"^dashboard/whatsapp/connect/hosted/media/",
]

# Instagram API with Instagram Login exposes a dedicated Instagram App ID and
# App Secret in Meta's Instagram API setup. They are not interchangeable with
# the generic Meta App ID used by WhatsApp Embedded Signup. Production requires
# the dedicated pair so SHVYA never redirects customers to Instagram with the
# wrong client_id.
META_INSTAGRAM_APP_ID = config(
    "META_INSTAGRAM_APP_ID",
    default="",
)
META_INSTAGRAM_APP_SECRET = config(
    "META_INSTAGRAM_APP_SECRET",
    default="",
)
# Load the existing environment key; otherwise the webhook getter silently
# falls back to the unrelated generic Meta/WhatsApp verification token.
META_INSTAGRAM_VERIFY_TOKEN = config("META_INSTAGRAM_VERIFY_TOKEN", default="")
META_INSTAGRAM_REQUIRE_DEDICATED_CREDENTIALS = True

# Reuse healthy PostgreSQL connections across Gunicorn requests instead of
# paying connection setup cost on every request. Keep the lifetime tunable
# for deployments that later introduce PgBouncer.
DATABASES["default"]["CONN_MAX_AGE"] = config(
    "DB_CONN_MAX_AGE",
    default=60,
    cast=int,
)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

MIDDLEWARE = [
    *MIDDLEWARE,
    "apps.core.middleware.GlobalToastMiddleware",
    "apps.core.whatsapp_theme.WhatsAppThemeMiddleware",
]
